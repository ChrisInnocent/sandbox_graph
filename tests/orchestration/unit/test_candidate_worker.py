import json
import pytest
from sandbox_graph.runtime.candidate_worker import CandidateWorker, CodingConfig
from sandbox_graph.runtime.agent_bridge import (
    CollectReply, ProviderScope, Usage, UsageCeiling, ProviderError, LookupReply)
from tests.orchestration.unit.test_agent_bridge import ControlledProvider,capabilities,receipt
from tests.orchestration.unit.test_evolution_service import setup,edits
from sandbox_graph.runtime.gate_runner import GateCase, TrustedGateRunner


async def verified(repository,candidate,token,expected, *, now=1000.):
    from pathlib import Path
    async def observe(context):
        return {'status':'pass' if (Path(context.candidate.workspace_ref)/'app.py').read_text()==expected else 'fail',
            'observations':{'expected':expected}}
    cases={gate+'/value':GateCase('1',observe) for gate in ('G0','G1','G2','G3','G4','G6.pre')}
    runner=TrustedGateRunner(repository,cases=cases,producer_identity='trusted',producer_version='1',
        runner_ref='runner/1',runner_version='1',runner_bundle_hash='a'*64)
    with repository.transaction() as tx:
        tx.put('trusted_gate_producers','trusted',{'version':'1','runner_ref':'runner/1','runner_version':'1',
            'bundle_hash':'a'*64,'revoked':False,'expires_at':(now+100)},immutable=True)
        tx.put('gate_profiles','trusted:1',{'profile_ref':'trusted:1','suite_hash':runner.suite_hash,'policy_hash':'b'*64,
            'cases':{gate:[gate+'/value'] for gate in ('G0','G1','G2','G3','G4','G6.pre')}},immutable=True)
        tx.put('gate_executions','gate/'+str(candidate.revision),{'thread_id':token.thread_id,
            'candidate_ref':candidate.candidate_ref,'profile_ref':'trusted:1','producer_identity':'trusted',
            'environment_ref':'test-controlled','environment_hash':'c'*64,'status':'running'},immutable=True)
    return await runner.run('gate/'+str(candidate.revision),token=token)


class Coder(ControlledProvider):
    def __init__(self,proposal):
        scope=ProviderScope(schema_version=1,kind='candidate',ref=proposal.proposal_id)
        super().__init__(capabilities(scopes=(scope,)))
        self.outputs={};self.records={}
    async def submit(self,env):
        self.submissions.append(env)
        index=len(self.submissions)
        self.saved=receipt(env,handle='task:'+str(index),receipt_id='receipt:'+str(index),state='succeeded',children_stopped=True,
            usage=Usage(schema_version=1,input_tokens=1,output_tokens=1,cost_microunits=1))
        self.records[env.request_id]=self.saved
        self.outputs[self.saved.handle]=CollectReply(schema_version=1,receipt=self.saved,
            output_json=json.dumps({'schema_version':1,'edits':edits(index+1,index)}))
        if self.lose_submit:raise ProviderError('timeout')
        return self.saved
    async def lookup(self,request_id):
        self.lookups.append(request_id)
        return LookupReply(schema_version=1,acceptance='unknown' if self.unknown_lookup else 'accepted',
            receipt=None if self.unknown_lookup else self.records[request_id])
    async def collect(self,handle):return self.outputs[handle]


def config():
    return CodingConfig(provider_id='local',model_id='coder',model_version='1',
        usage_ceiling=UsageCeiling(schema_version=1,input_tokens=10,output_tokens=10,cost_microunits=10),
        graph_prefix='candidate-graph',max_observations=3,max_context_bytes=65536)


@pytest.mark.asyncio
async def test_real_structured_edits_failed_gate_then_second_revision_pass(repository,tmp_path):
    service,proposal,target,token,repo=setup(repository,tmp_path)
    service.open(proposal,target=target,token=token);coder=Coder(proposal)
    calls=[]
    async def verify(candidate,token):
        calls.append(candidate.candidate_ref)
        # Controlled external oracle reads frozen bytes; the model supplies no verdict.
        return await verified(repository,candidate,token,'VALUE=3\n')
    worker=CandidateWorker(service,coder,config=config(),verify=verify)
    first=await worker.tick(proposal.proposal_id,token=token)
    assert first['status']=='tests_failed'
    second=await worker.tick(proposal.proposal_id,token=token)
    assert second['status']=='awaiting_independent_review'
    assert await worker.tick(proposal.proposal_id,token=token)==second
    assert len(coder.submissions)==2 and len(calls)==2
    assert (repo/'app.py').read_text()=='VALUE=1\n'
    assert json.loads(coder.submissions[1].input_json)['previous_gate']['status']=='fail'


@pytest.mark.asyncio
async def test_response_loss_observes_original_request_without_new_revision_or_budget_reset(repository,tmp_path):
    service,proposal,target,token,_=setup(repository,tmp_path)
    service.open(proposal,target=target,token=token);coder=Coder(proposal)
    coder.lose_submit=True;coder.unknown_lookup=True
    async def verify(candidate,token):return await verified(repository,candidate,token,'VALUE=2\n')
    worker=CandidateWorker(service,coder,config=config(),verify=verify)
    assert (await worker.tick(proposal.proposal_id,token=token))['status']=='provider_unknown'
    assert (await worker.tick(proposal.proposal_id,token=token))['status']=='provider_unknown'
    coder.unknown_lookup=False
    assert (await worker.tick(proposal.proposal_id,token=token))['status']=='awaiting_independent_review'
    assert len(coder.submissions)==1
    assert len(service.status(proposal.proposal_id)['candidate_refs'])==1


@pytest.mark.asyncio
async def test_cancelled_candidate_does_not_call_provider_or_trusted_verifier(repository,tmp_path):
    service,proposal,target,token,_=setup(repository,tmp_path)
    service.open(proposal,target=target,token=token);coder=Coder(proposal)
    async def verify(candidate,token):raise AssertionError('must not run')
    worker=CandidateWorker(service,coder,config=config(),verify=verify)
    service.cancel(proposal.proposal_id,token=token)
    result=await worker.tick(proposal.proposal_id,token=token)
    assert result['status']=='cancelled' and not coder.submissions

@pytest.mark.asyncio
async def test_candidate_provider_has_owned_lifecycle_inventory(repository,tmp_path):
    from sandbox_graph.services.lifecycle_inventory import LifecycleInventory
    service,proposal,target,token,_=setup(repository,tmp_path);service.open(proposal,target=target,token=token)
    coder=Coder(proposal);coder.lose_submit=True;coder.unknown_lookup=True
    worker=CandidateWorker(service,coder,config=config(),verify=None)
    await worker.tick(proposal.proposal_id,token=token)
    with repository.transaction() as tx:
        inventory=LifecycleInventory.collect_in(tx,run_ref='run/a',scope_kind='work',scope_ref='work/a')
        item=next(row for row in inventory.items if row.kind=='agent_provider_requests')
        assert item.ownership=='owned' and not item.closed

@pytest.mark.asyncio
@pytest.mark.parametrize('output',['not-json','{"schema_version":1,"edits":[]}',
    '{"schema_version":1,"schema_version":1,"edits":[]}'])
async def test_invalid_coding_response_is_durable_without_repeated_model_calls(repository,tmp_path,output):
    service,proposal,target,token,_=setup(repository,tmp_path);service.open(proposal,target=target,token=token)
    class Invalid(Coder):
        async def collect(self,handle):
            original=await super().collect(handle)
            return original.model_copy(update={'output_json':output})
    coder=Invalid(proposal);worker=CandidateWorker(service,coder,config=config(),verify=None)
    first=await worker.tick(proposal.proposal_id,token=token)
    assert first['status']=='invalid_response'
    assert await worker.tick(proposal.proposal_id,token=token)==first
    assert len(coder.submissions)==1
    with repository.transaction() as tx:
        assert not tx.scan('candidate_operations') and not tx.scan('candidate_revisions')

@pytest.mark.asyncio
async def test_unchanged_patch_stops_without_dangling_edit_intent(repository,tmp_path):
    service,proposal,target,token,_=setup(repository,tmp_path);service.open(proposal,target=target,token=token)
    class Unchanged(Coder):
        async def collect(self,handle):
            original=await super().collect(handle)
            return original.model_copy(update={'output_json':json.dumps({'schema_version':1,'edits':edits(1)})})
    coder=Unchanged(proposal);worker=CandidateWorker(service,coder,config=config(),verify=None)
    first=await worker.tick(proposal.proposal_id,token=token)
    assert first['status']=='no_progress'
    assert await worker.tick(proposal.proposal_id,token=token)==first
    assert service.status(proposal.proposal_id)['active_operation'] is None
    assert len(coder.submissions)==1
