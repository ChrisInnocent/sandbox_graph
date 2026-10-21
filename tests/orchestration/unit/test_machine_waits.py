"""P5.2 / ORC-038: machine waits never lose or busy-poll a wakeup."""
import pytest

from sandbox_graph.persistence.repository import Conflict
from sandbox_graph.runtime.events import EventEnvelope, EventService
from sandbox_graph.runtime.waits import WaitRecord, WaitService


def wait(**extra):
    data = dict(wait_id='w1', activation_id='a', thread_id='t', scope='g', plan_ref='p',
                graph_ref='g', version=1, deadline=10 ** 9, aggregate_id='agg', cursor=0)
    data.update(extra)
    return WaitRecord(**data)


def event(seq):
    return EventEnvelope(event_id=f'e{seq}', producer_scope='s', producer_epoch=1,
                         aggregate_id='agg', aggregate_seq=seq, run_ref='r', plan_ref='p',
                         graph_ref='g', kind='k', payload={})


def test_park_is_waiting_then_ready_when_the_next_event_arrives(repository):
    service = WaitService(repository)
    assert service.park(wait()) == 'waiting'
    EventService(repository).append(event(1))
    assert service.park(wait(wait_id='w2', cursor=0)) == 'ready'   # event at cursor+1 present
    assert service.wake('w2', version=1) == 't'
    assert service.wake('w2', version=1) is None                   # already resumed, no re-notify


def test_park_expires_immediately_when_deadline_already_passed(repository):
    service = WaitService(repository)
    assert service.park(wait(deadline=1)) == 'expired'            # now == 1000


def test_wake_requires_an_existing_event_and_the_exact_version(repository):
    service = WaitService(repository)
    service.park(wait())                                          # cursor 0, no event yet
    assert service.wake('w1', version=1) is None                  # no event -> no wakeup
    with pytest.raises(Conflict):
        service.wake('w1', version=2)                             # stale version
    with pytest.raises(Conflict):
        service.park(wait(scope='other'))                         # wait identity conflict
