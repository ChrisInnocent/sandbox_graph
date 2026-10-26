import pytest
from tests.orchestration.unit.test_evolution_service import setup
from tests.orchestration.unit.test_candidate_worker import Coder,config
from sandbox_graph.runtime.candidate_worker import CandidateWorker
from sandbox_graph.runtime.candidate_safety import CandidateSafety
from sandbox_graph.runtime.agent_bridge import CancelReply,ProviderError
from sandbox_graph.runtime.tasks import TaskBackend
from sandbox_graph.runtime.worker import Worker,WorkerPolicy

class Running(Coder):
    def __init__(self,proposal,stopped):super().__init__(proposal);self.stopped=stopped;self.cancels=0
    async def submit(self,env):
        value=await super().submit(env)
        self.saved=value.model_copy(update={'state':'running','children_stopped':False,'receipt_id':'running','usage':None})
        self.records[env.request_id]=self.saved
        return self.saved
    async def cancel(self,handle):
        self.cancels+=1
        if self.stopped:self.current=self.saved.model_copy(update={'state':'cancelled','children_stopped':True,'receipt_id':'stopped'})
        return CancelReply(schema_version=1,request_id=self.saved.request_id,scope=self.saved.scope,handle=handle,accepted=True)

@pytest.mark.asyncio
@pytest.mark.parametrize('stopped',[False,True])
async def test_candidate_cancel_preserves_original_request_and_requires_children_stopped(repository,tmp_path,stopped):
    service,proposal,target,token,_=setup(repository,tmp_path);service.open(proposal,target=target,token=token)
    provider=Running(proposal,stopped);coding=CandidateWorker(service,provider,config=config(),verify=None)
    assert (await coding.tick(proposal.proposal_id,token=token))['status']=='provider_running'
    service.cancel(proposal.proposal_id,token=token)
    with repository.transaction() as tx:
        tx.release_thread(token)
        tx.put('evolution_features','run/a',{'candidate_enabled':False})
        tx.put('lifecycle_rollouts','run/a',{'mode':'off','scopes':[]})
        tx.put('dispatch_gates','run/a',{'open':False,'generation':3})
    safety=CandidateSafety(service,{'local':provider})
    worker=Worker(repository,{'candidate_cleanup':safety},policy=WorkerPolicy(claim_seconds=30,job_seconds=10,poll_seconds=.1))
    assert await worker.run_once()
    with repository.transaction() as tx:
        progress=tx.get('candidate_progress',proposal.proposal_id)['body']
        assert (progress.get('cleanup_status')=='complete')==stopped
        assert len(tx.scan('agent_provider_requests'))==1
    assert provider.cancels==1 and len(provider.submissions)==1


def test_candidate_cancel_targets_only_its_lifecycle_and_preserves_run_dispatch(repository,tmp_path):
    from sandbox_graph.services.lifecycle_stop import LifecycleStopService
    from sandbox_graph.services.agent_lifecycle import LifecycleService
    service,proposal,target,token,_=setup(repository,tmp_path);service.open(proposal,target=target,token=token)
    with repository.transaction() as tx:tx.put('dispatch_gates','run/a',{'open':True,'generation':1})
    service.cancel(proposal.proposal_id,token=token)
    with repository.transaction() as tx:
        life=LifecycleService.load_in(tx,proposal.source_key.lifecycle_id)
        assert LifecycleStopService.policy_in(tx,life)=='cancel'
        other=life.model_copy(update={'key':life.key.model_copy(update={'lifecycle_id':'other'})})
        assert LifecycleStopService.policy_in(tx,other) is None
        assert tx.get('dispatch_gates','run/a')['body']['open']

@pytest.mark.asyncio
async def test_cancel_retains_partial_edit_evidence_and_closes_known_local_mutation(repository,tmp_path):
    from tests.orchestration.unit.test_evolution_service import edits
    from sandbox_graph.services.lifecycle_budgets import LifecycleBudgetService
    service,proposal,target,token,_=setup(repository,tmp_path);service.open(proposal,target=target,token=token)
    with repository.transaction() as tx:
        LifecycleBudgetService.reserve_in(tx,request_id='candidate-revision:edit/partial',lifecycle_id=proposal.source_key.lifecycle_id,
            amounts={'candidate_revisions':1},input_ref='partial')
        tx.put('candidate_operations','edit/partial',{'command':{'proposal_id':proposal.proposal_id,'edits':edits(2),'graph_ref':'g:2'},
            'revision':1,'reservation':'candidate-revision:edit/partial','status':'started'})
        row=tx.get('candidate_progress',proposal.proposal_id)
        tx.put('candidate_progress',proposal.proposal_id,row['body']|{'active_operation':'edit/partial'})
    (service.workspace._folder(proposal.proposal_id)/'work/app.py').write_text('VALUE=2\n')
    service.cancel(proposal.proposal_id,token=token)
    with repository.transaction() as tx:tx.release_thread(token)
    worker=Worker(repository,{'candidate_cleanup':CandidateSafety(service,{})},
        policy=WorkerPolicy(claim_seconds=30,job_seconds=10,poll_seconds=.1,accepted_job_kinds=('candidate_cleanup',)))
    assert await worker.run_once()
    with repository.transaction() as tx:
        row=tx.get('candidate_operations','edit/partial')['body']
        assert row['status']=='cancelled' and row['disposition']['paths'][0]['state']=='after'
        assert tx.get('candidate_progress',proposal.proposal_id)['body']['active_operation'] is None
        assert not tx.scan('candidate_revisions')

@pytest.mark.asyncio
async def test_cancel_settles_reserved_but_never_submitted_model_request(repository,tmp_path,monkeypatch):
    from sandbox_graph.runtime.agent_bridge import AgentBridge
    from sandbox_graph.services.lifecycle_budgets import LifecycleBudgetService
    service,proposal,target,token,_=setup(repository,tmp_path);service.open(proposal,target=target,token=token)
    provider=Coder(proposal);coding=CandidateWorker(service,provider,config=config(),verify=None)
    async def before_submission(*args,**kwargs):raise OSError('controller died before bridge intent')
    monkeypatch.setattr(AgentBridge,'submit',before_submission)
    with pytest.raises(OSError):await coding.tick(proposal.proposal_id,token=token)
    service.cancel(proposal.proposal_id,token=token)
    with repository.transaction() as tx:tx.release_thread(token)
    worker=Worker(repository,{'candidate_cleanup':CandidateSafety(service,{})},
        policy=WorkerPolicy(claim_seconds=30,job_seconds=10,poll_seconds=.1,accepted_job_kinds=('candidate_cleanup',)))
    assert await worker.run_once()
    with repository.transaction() as tx:
        state=LifecycleBudgetService.snapshot_in(tx,proposal.source_key.lifecycle_id)
        assert all(row['reserved']['tokens']==0 and row['reserved']['cost']==0 for row in state.values())
        assert tx.scan('candidate_unsubmitted_proofs') and not tx.scan('agent_provider_requests')
    assert not provider.submissions


@pytest.mark.asyncio
async def test_isolation_cleanup_consumes_independent_safety_budget(repository,tmp_path):
    from sandbox_graph.services.lifecycle_budgets import LifecycleBudgetService
    service,proposal,target,token,_=setup(repository,tmp_path);service.open(proposal,target=target,token=token)
    class Backend:
        calls=[]
        async def cancel(self,request_id,*,timeout_s):
            self.calls.append(request_id)
            return {'cleanup':'complete','request_id':request_id}
    backend=Backend()
    with repository.transaction() as tx:
        tx.put('candidate_isolations','original-isolation',{'proposal_id':proposal.proposal_id,
            'backend_id':'bwrap','request_id':'original-isolation'})
        before=LifecycleBudgetService.snapshot_in(tx,proposal.source_key.lifecycle_id)['lineage']['safety_spent']['stop_calls']
    service.cancel(proposal.proposal_id,token=token)
    with repository.transaction() as tx:tx.release_thread(token)
    worker=Worker(repository,{'candidate_cleanup':CandidateSafety(service,{},isolations={'bwrap':backend})},
        policy=WorkerPolicy(claim_seconds=30,job_seconds=10,poll_seconds=.1,accepted_job_kinds=('candidate_cleanup',)))
    assert await worker.run_once()
    with repository.transaction() as tx:
        assert LifecycleBudgetService.snapshot_in(tx,proposal.source_key.lifecycle_id)['lineage']['safety_spent']['stop_calls']==before+1
        assert tx.get('candidate_disposition_proofs','original-isolation')
    assert backend.calls==['original-isolation']
