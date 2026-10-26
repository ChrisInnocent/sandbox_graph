import pytest
from sandbox_graph.runtime.evolution_dispatch import EvolutionCoordinator
from sandbox_graph.runtime.candidate_worker import CandidateWorker
from sandbox_graph.runtime.tasks import TaskBackend
from sandbox_graph.services.agent_lifecycle import LifecycleService
from tests.orchestration.unit.test_evolution_service import setup
from tests.orchestration.unit.test_candidate_worker import Coder,config,verified


@pytest.mark.asyncio
async def test_persistent_candidate_job_edits_and_enqueues_next_revision(repository,tmp_path):
    service,proposal,target,token,_=setup(repository,tmp_path)
    coder=Coder(proposal)
    async def verify(candidate,token):return await verified(repository,candidate,token,'VALUE=3\n')
    worker=CandidateWorker(service,coder,config=config(),verify=verify)
    coordinator=EvolutionCoordinator(service,worker,bindings=[{'proposal':proposal,'target':target}],lease_seconds=60)
    with repository.transaction() as tx:
        life=LifecycleService.load_in(tx,proposal.source_key.lifecycle_id)
        result=coordinator.request_in(tx,{'route':'needs_code','decision_ref':'decision/1'},life,token=token)
        assert coordinator.request_in(tx,{'route':'needs_code','decision_ref':'decision/1'},life,token=token)==result
        tx.release_thread(token)
    task=TaskBackend(repository);job=task.claim('candidate-worker',30)
    first=await coordinator(job)
    assert first['candidate_status']=='tests_failed' and first['next_job_ref']
    task.finish(job['job_id'],owner_epoch=job['owner_epoch'],result=first)
    job=task.claim('candidate-worker',30)
    second=await coordinator(job)
    assert second['candidate_status']=='awaiting_independent_review'
    assert len(coder.submissions)==2
    with repository.transaction() as tx:
        assert len(tx.scan('candidate_revisions'))==2
        assert len(tx.scan('candidate_job_results'))==2


def test_missing_binding_is_explicit_and_wrong_scope_cannot_enqueue(repository,tmp_path):
    service,proposal,target,token,_=setup(repository,tmp_path)
    coordinator=EvolutionCoordinator(service,None,bindings=[],lease_seconds=60)
    with repository.transaction() as tx:
        life=LifecycleService.load_in(tx,proposal.source_key.lifecycle_id)
        result=coordinator.request_in(tx,{'route':'needs_code','decision_ref':'decision/1'},life,token=token)
        assert result['status']=='waiting_dependency' and result['reason']=='candidate_scope_binding_missing'
        assert not tx.scan('jobs')
