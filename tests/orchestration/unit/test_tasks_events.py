"""D18.2 / ORC-030,033,037-040: durable jobs, dedupe, contiguous events."""
import pytest

from sandbox_graph.persistence.repository import Conflict, StaleOwner
from sandbox_graph.runtime.events import EventEnvelope, EventService
from sandbox_graph.runtime.tasks import JobSpec, TaskBackend


def spec(**extra):
    data = dict(job_id='j1', job_kind='work', scope_ref='w', thread_id='t', activation_id='a',
                dedupe_key='d', due_at=0.0, deadline=10 ** 9)
    data.update(extra)
    return JobSpec(**data)


def event(**extra):
    data = dict(event_id='e1', producer_scope='s', producer_epoch=1, aggregate_id='agg',
                aggregate_seq=1, run_ref='r', plan_ref='p', graph_ref='g', kind='k', payload={})
    data.update(extra)
    return EventEnvelope(**data)


def test_enqueue_dedupes_on_key_and_conflicts_on_diff_input(repository):
    backend = TaskBackend(repository)
    assert backend.enqueue(spec()) == 'j1'
    assert backend.enqueue(spec(job_id='j2')) == 'j1'          # same key, same input -> original
    with pytest.raises(Conflict):
        backend.enqueue(spec(job_id='j3', payload={'x': 1}))   # same key, different input
    with pytest.raises(Conflict):
        backend.enqueue(spec(job_id='j1', dedupe_key='other'))  # job id reused under new key


def test_claim_fails_expired_job_instead_of_dropping_it(repository):
    backend = TaskBackend(repository)
    backend.enqueue(spec(deadline=999.0))   # deadline is before clock now (1000)
    assert backend.claim('owner', 5) is None
    assert repository.rows[('jobs', 'j1')]['body']['status'] == 'failed'
    assert repository.rows[('jobs', 'j1')]['body']['reason'] == 'deadline_or_attempt_budget'


def test_claim_renew_finish_are_epoch_fenced(repository):
    backend = TaskBackend(repository)
    backend.enqueue(spec())
    claimed = backend.claim('owner', 5)
    assert claimed['status'] == 'running'
    with pytest.raises(StaleOwner):
        backend.renew_claim('j1', owner_epoch=claimed['owner_epoch'] - 1, ttl=5)
    backend.finish('j1', owner_epoch=claimed['owner_epoch'], result={'execution_status': 'succeeded'})
    backend.finish('j1', owner_epoch=claimed['owner_epoch'], result={'execution_status': 'succeeded'})
    with pytest.raises(Conflict):
        backend.finish('j1', owner_epoch=claimed['owner_epoch'], result={'execution_status': 'failed'})


def test_closed_dispatch_gate_supersedes_ordinary_but_not_cleanup(repository):
    backend = TaskBackend(repository)
    repository.rows[('dispatch_gates', 'r')] = {'key': 'r', 'revision': 0,
                                               'body': {'open': False, 'generation': 1}}
    backend.enqueue(spec(job_id='work', run_id='r', dedupe_key='d1'))
    backend.enqueue(spec(job_id='cleanup', job_kind='cleanup', run_id='r', dedupe_key='d2'))
    claimed = []
    while (job := backend.claim('owner', 5)) is not None:
        claimed.append(job['job_id'])
    assert repository.rows[('jobs', 'work')]['body']['status'] == 'superseded'
    assert 'cleanup' in claimed          # cleanup is not gated


def test_event_sequence_and_id_conflicts(repository):
    service = EventService(repository)
    service.append(event())
    service.append(event())                                  # idempotent same content
    with pytest.raises(Conflict):
        service.append(event(payload={'x': 1}))              # same id, different content
    with pytest.raises(Conflict):
        service.append(event(event_id='e2', payload={'y': 1}))  # same seq, different content


def test_consume_stops_at_a_gap_and_checks_producer_scope(repository):
    service = EventService(repository)
    service.append(event(event_id='e1', aggregate_seq=1))
    service.append(event(event_id='e3', aggregate_seq=3))    # gap at seq 2
    accepted = service.consume('c', 'agg', allowed_scopes={'s'})
    assert [e.aggregate_seq for e in accepted] == [1]        # never skips the gap
    with pytest.raises(PermissionError):
        service.consume('c2', 'agg', allowed_scopes={'other'})
