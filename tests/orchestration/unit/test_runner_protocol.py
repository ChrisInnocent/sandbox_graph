import asyncio
import pytest
from sandbox_graph.runtime.runner import OperationService, RunnerStatus, RunnerCollection, CleanupResult
from sandbox_graph.domain.models import Operation, ScriptRef, canonical_hash, RunRecord
from sandbox_graph.runtime.leases import LeaseService
from sandbox_graph.services.revisions import RevisionService
from sandbox_graph.persistence.repository import Conflict

class DurableRunnerFixture:
    """Synthetic runner ledger survives constructing a new controller service."""
    def __init__(self): self.effects={}; self.lose_response=False; self.unknown=False
    async def submit(self,operation):
        if operation.operation_id not in self.effects:
            self.effects[operation.operation_id]=operation.input_hash
        if self.lose_response: raise ConnectionError('accepted but response lost')
        return RunnerStatus(state='running',task_handle=operation.operation_id,authoritative=True)
    async def reconcile(self,operation_id,scope):
        if self.unknown: return RunnerStatus(state='unknown')
        return RunnerStatus(state='completed' if operation_id in self.effects else 'not_submitted',task_handle=operation_id,authoritative=True)
    async def cancel(self,operation_id,scope,reason): return RunnerStatus(state='cancelled',authoritative=True)
    async def collect(self,operation_id,scope,last_seq=0): return RunnerCollection(events=(),last_seq=last_seq)
    async def cleanup(self,scope,resource_ids): return CleanupResult(status='complete',resource_ids=resource_ids)


def service(repository):
    RevisionService(repository).create(RunRecord(run_id='r',root_thread_id='t',graph_ref='g',contract_ref='c',active_plan_ref='p',auth_ref='a',lineage_id='r'))
    token=LeaseService(repository).acquire_thread('t','worker',100)
    runner=DurableRunnerFixture(); script=ScriptRef(script_id='bench',version='1',digest='a'*64)
    op=Operation(operation_id='op',input_hash=canonical_hash({'Q':5}),script_ref=script,scope='G_A',input={'Q':5},deadline=1050,run_id='r')
    return OperationService(repository,runner,{('bench','1'):script}),runner,op,token


def test_response_loss_reconciles_original_identity_without_resubmit(repository):
    s,runner,op,token=service(repository); runner.lose_response=True
    assert asyncio.run(s.submit(op,token)).state=='unknown'
    recovered=OperationService(repository,runner,s.scripts)
    result=asyncio.run(recovered.submit(op,token))
    assert result.state=='completed'
    assert runner.effects=={'op':canonical_hash({'Q':5})}
    with repository.transaction() as tx:
        assert tx.get('operations','op')['body']['phase']=='terminal'


def test_unknown_never_retries_and_unregistered_script_has_no_effect(repository):
    s,runner,op,token=service(repository); runner.lose_response=True
    asyncio.run(s.submit(op,token)); runner.unknown=True
    assert asyncio.run(s.submit(op,token)).state=='unknown'
    bad=op.model_copy(update={'script_ref':ScriptRef(script_id='shell',version='1',digest='b'*64)})
    with pytest.raises(ValueError,match='registered'): asyncio.run(s.submit(bad,token))
    assert len(runner.effects)==1


def test_budget_payload_identity_cannot_change_on_replay(repository):
    s,runner,op,token=service(repository)
    asyncio.run(s.submit(op,token))
    changed=op.model_copy(update={'input':{'Q':6},'input_hash':canonical_hash({'Q':6})})
    with pytest.raises(Conflict): asyncio.run(s.submit(changed,token))


@pytest.mark.parametrize('policy',['cancel','drain'])
def test_unstarted_permit_cannot_submit_after_version_cutoff(repository,policy):
    s,runner,op,token=service(repository)
    RevisionService(repository).permit('r','op',generation=1,scope='G_A')
    RevisionService(repository).activate_revision(request_id='cutoff',predecessor_run_id='r',
        expected_revision=0,candidate_plan_hash='p2',graph_ref='g2',disposition_policy=policy)
    with pytest.raises(Conflict,match='gate|superseded'):
        asyncio.run(s.submit(op,token))
    assert runner.effects=={}
    with repository.transaction() as tx:
        assert tx.get('operations','op')['body']['phase']=='intent'
