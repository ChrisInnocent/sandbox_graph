import json
import pytest
from sandbox_graph.runtime.candidate_review_worker import CandidateReviewWorker
from sandbox_graph.runtime.agent_bridge import CollectReply,Usage
from tests.orchestration.unit.test_candidate_worker import Coder,config,verified
from tests.orchestration.unit.test_agent_bridge import receipt
from tests.orchestration.unit.test_evolution_service import setup,edits
from sandbox_graph.services.candidate_reviews import CandidateReviewService

class Reviewer(Coder):
    async def submit(self,env):
        self.submissions.append(env)
        self.saved=receipt(env,handle='review:'+env.request_id,receipt_id='review-receipt:'+env.request_id,
            state='succeeded',children_stopped=True,
            usage=Usage(schema_version=1,input_tokens=1,output_tokens=1,cost_microunits=1))
        self.records[env.request_id]=self.saved
        self.outputs[self.saved.handle]=CollectReply(schema_version=1,receipt=self.saved,
            output_json=json.dumps({'schema_version':1,'result':'approved','findings':[]}))
        return self.saved

@pytest.mark.asyncio
async def test_independent_reviewer_consumes_frozen_diff_and_trusted_gate_and_reuses_request(repository,tmp_path):
    service,proposal,target,token,_=setup(repository,tmp_path);service.open(proposal,target=target,token=token)
    candidate=service.edit_revision(proposal.proposal_id,request_id='edit/1',edits=edits(2),graph_ref='g:2',token=token)
    gate=await verified(repository,candidate,token,'VALUE=2\n')
    with repository.transaction() as tx:
        tx.put('candidate_reviewers','reviewer',{'kind':'model','version':'1','revoked':False,'expires_at':1100.,
            'provider_id':'local','model_id':'coder','model_version':'1'},immutable=True)
    provider=Reviewer(proposal)
    worker=CandidateReviewWorker(service,provider,config=config(),reviewer_identity='reviewer',coder_identity='coder')
    result=await worker.review(candidate,gate['manifest_ref'],token=token)
    assert result['status']=='review_approved'
    assert await worker.review(candidate,gate['manifest_ref'],token=token)==result
    assert len(provider.submissions)==1
    context=json.loads(provider.submissions[0].input_json)
    assert context['role']=='reviewer' and 'edits' not in context['response_contract']
    with repository.transaction() as tx:
        assert CandidateReviewService.approved_in(tx,result['review_ref'],candidate).result=='approved'

@pytest.mark.asyncio
async def test_revoked_reviewer_never_calls_provider(repository,tmp_path):
    service,proposal,target,token,_=setup(repository,tmp_path);service.open(proposal,target=target,token=token)
    candidate=service.edit_revision(proposal.proposal_id,request_id='edit/1',edits=edits(2),graph_ref='g:2',token=token)
    gate=await verified(repository,candidate,token,'VALUE=2\n');provider=Reviewer(proposal)
    worker=CandidateReviewWorker(service,provider,config=config(),reviewer_identity='missing',coder_identity='coder')
    with pytest.raises(PermissionError):await worker.review(candidate,gate['manifest_ref'],token=token)
    assert not provider.submissions

@pytest.mark.asyncio
async def test_coding_tick_dispatches_registered_independent_reviewer(repository,tmp_path):
    from sandbox_graph.runtime.candidate_worker import CandidateWorker
    service,proposal,target,token,_=setup(repository,tmp_path);service.open(proposal,target=target,token=token)
    with repository.transaction() as tx:
        tx.put('candidate_reviewers','reviewer',{'kind':'model','version':'1','revoked':False,'expires_at':1100.,
            'provider_id':'local','model_id':'coder','model_version':'1'},immutable=True)
    reviewer=CandidateReviewWorker(service,Reviewer(proposal),config=config(),reviewer_identity='reviewer',coder_identity='coder')
    async def verify(candidate,owner):return await verified(repository,candidate,owner,'VALUE=2\n')
    worker=CandidateWorker(service,Coder(proposal),config=config(),verify=verify,reviewer=reviewer)
    result=await worker.tick(proposal.proposal_id,token=token)
    assert result['status']=='review_approved'

@pytest.mark.asyncio
async def test_approved_with_blocking_findings_requests_revision_and_moves_lifecycle(repository,tmp_path):
    from sandbox_graph.runtime.candidate_worker import CandidateWorker
    from sandbox_graph.services.agent_lifecycle import LifecycleService
    class Blocking(Reviewer):
        async def collect(self,handle):
            value=await super().collect(handle)
            request=next(item for item in self.submissions if self.records[item.request_id].handle==handle)
            candidate=json.loads(request.input_json)['candidate_ref']
            return value.model_copy(update={'output_json':json.dumps({'schema_version':1,'result':'approved',
                'findings':[{'finding_ref':'blocking:1','severity':'blocking','summary':'contract regression',
                    'evidence_refs':[candidate]}]})})
    service,proposal,target,token,_=setup(repository,tmp_path);service.open(proposal,target=target,token=token)
    with repository.transaction() as tx:
        tx.put('candidate_reviewers','reviewer',{'kind':'model','version':'1','revoked':False,'expires_at':1100.,
            'provider_id':'local','model_id':'coder','model_version':'1'},immutable=True)
    reviewer=CandidateReviewWorker(service,Blocking(proposal),config=config(),reviewer_identity='reviewer',coder_identity='coder')
    async def verify(candidate,owner):return await verified(repository,candidate,owner,'VALUE=2\n')
    worker=CandidateWorker(service,Coder(proposal),config=config(),verify=verify,reviewer=reviewer)
    result=await worker.tick(proposal.proposal_id,token=token)
    assert result['status']=='review_changes_requested'
    with repository.transaction() as tx:
        assert LifecycleService.load_in(tx,proposal.source_key.lifecycle_id).phase=='candidate_building'
