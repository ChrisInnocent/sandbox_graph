import pytest
from sandbox_graph.persistence.repository import Conflict, StaleOwner
from sandbox_graph.runtime.leases import LeaseService
from sandbox_graph.runtime.tasks import TaskBackend, JobSpec
from sandbox_graph.runtime.events import EventService, EventEnvelope
from sandbox_graph.runtime.waits import WaitService, WaitRecord, DecisionEnvelope
from sandbox_graph.services.revisions import RevisionService
from sandbox_graph.domain.models import RunRecord


def test_reservations_are_all_or_none_and_expiry_does_not_free_resources(repository,clock):
    service=LeaseService(repository)
    a=service.acquire_thread('a','owner',10)
    service.acquire_all(('host_A','pool_A'),scope='A',token=a)
    b=service.acquire_thread('b','owner',100)
    with pytest.raises(Conflict): service.acquire_all(('host_B','pool_A'),scope='B',token=b)
    service.acquire_all(('host_B',),scope='B',token=b)
    clock.advance(11)
    with pytest.raises(StaleOwner): service.renew(a,10)
    a2=service.acquire_thread('a','owner2',10)
    with pytest.raises(Conflict): service.acquire_all(('host_A',),scope='B',token=b)
    service.quarantine(('host_A',),scope='A',reason='effect unknown',token=a2)
    with pytest.raises(Conflict): service.release(('host_A',),scope='A',token=a2,closed=False)


def test_task_dedupe_claim_epoch_and_completion_receipt(repository,clock):
    q=TaskBackend(repository)
    spec=JobSpec(job_id='j',job_kind='work',scope_ref='G_A',thread_id='t',activation_id='a',dedupe_key='a:work',due_at=1000,deadline=1100)
    assert q.enqueue(spec)=='j'
    assert q.enqueue(spec)=='j'
    first=q.claim('one',ttl=5)
    assert q.claim('two',ttl=5) is None
    clock.advance(6)
    second=q.claim('two',ttl=5)
    assert second['owner_epoch']==2
    with pytest.raises(StaleOwner): q.finish('j',owner_epoch=first['owner_epoch'],result={'ok':True})
    q.finish('j',owner_epoch=second['owner_epoch'],result={'ok':True})
    assert q.claim('three',ttl=5) is None


def event(seq, payload=None):
    return EventEnvelope(event_id=f'e{seq}',producer_scope='G_A',producer_epoch=1,aggregate_id='group:A',aggregate_seq=seq,run_ref='r',plan_ref='p',graph_ref='g',kind='delivery',payload=payload or {'version':seq})


def test_event_gaps_duplicate_and_conflict_keep_projection_accurate(repository):
    e=EventService(repository)
    e.append(event(2))
    assert e.consume('root','group:A',allowed_scopes={'G_A'}) == []
    e.append(event(1)); e.append(event(1))
    assert [x.aggregate_seq for x in e.consume('root','group:A',allowed_scopes={'G_A'})]==[1,2]
    assert e.consume('root','group:A',allowed_scopes={'G_A'})==[]
    with pytest.raises(Conflict): e.append(event(1,{'version':99}))


def test_machine_wait_does_not_sleep_past_existing_event(repository):
    e=EventService(repository); w=WaitService(repository)
    e.append(event(1))
    wait=WaitRecord(wait_id='w',activation_id='fixture-activation',thread_id='t',scope='G_A',plan_ref='p',graph_ref='g',version=1,aggregate_id='group:A',cursor=0,deadline=1010)
    assert w.park(wait)=='ready'
    assert w.wake('w',version=1)=='t'
    assert w.wake('w',version=1) is None


def test_wrong_or_expired_answer_never_approves_wait(repository,clock):
    w=WaitService(repository)
    wait=WaitRecord(wait_id='w',activation_id='fixture-activation',thread_id='t',scope='G_A',plan_ref='p',graph_ref='g',version=1,question_id='q',interrupt_id='i',deadline=1010)
    w.park(wait)
    answer=DecisionEnvelope(decision_id='d',wait_id='w',wait_version=1,scope='G_A',plan_ref='old',graph_ref='g',question_id='q',interrupt_id='i',actor='human',answer={'approved':True})
    with pytest.raises(Conflict): w.accept(answer)
    clock.advance(11)
    assert w.watchdog()==['w']
    with pytest.raises(Conflict): w.accept(answer.model_copy(update={'plan_ref':'p'}))


def test_revision_switch_is_idempotent_and_closes_old_submission_gate(repository):
    service=RevisionService(repository)
    run=RunRecord(run_id='r',root_thread_id='t',graph_ref='g1',contract_ref='c',active_plan_ref='p1',auth_ref='auth',lineage_id='r')
    service.create(run)
    service.permit('r','op1',generation=1,scope='G_A')
    successor=service.activate_revision(request_id='change',predecessor_run_id='r',expected_revision=0,candidate_plan_hash='p2',graph_ref='g2',disposition_policy='drain')
    assert successor.run_id!='r' and successor.root_thread_id!='t'
    assert successor.supersedes_run_id=='r'
    assert service.activate_revision(request_id='change',predecessor_run_id='r',expected_revision=0,candidate_plan_hash='p2',graph_ref='g2',disposition_policy='drain')==successor
    with pytest.raises(Conflict): service.permit('r','op2',generation=1,scope='G_A')
    assert service.permit('r','op1',generation=1,scope='G_A')=='op1'
    assert service.permit('r','cleanup1',generation=1,scope='G_A',kind='cleanup')=='cleanup1'
    with pytest.raises(Conflict): service.activate_revision(request_id='other',predecessor_run_id='r',expected_revision=0,candidate_plan_hash='p3',graph_ref='g3',disposition_policy='drain')


@pytest.mark.parametrize('ttl',[float('nan'),float('inf'),True,0,-1])
def test_public_lease_and_claim_reject_nonfinite_or_non_numeric_ttl(repository,ttl):
    with pytest.raises(ValueError): LeaseService(repository).acquire_thread('t','owner',ttl)
    with pytest.raises(ValueError): TaskBackend(repository).claim('owner',ttl)
