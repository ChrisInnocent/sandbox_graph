"""P5.4 / ORC-035,043,045: wait TTL/watchdog; timeout never approves."""
import pytest

from sandbox_graph.persistence.repository import Conflict
from sandbox_graph.runtime.waits import DecisionEnvelope, WaitRecord, WaitService


def wait(**extra):
    data = dict(wait_id='w1', activation_id='a', thread_id='t', scope='g', plan_ref='p',
                graph_ref='g', version=1, deadline=2000, question_id='q', interrupt_id='i')
    data.update(extra)
    return WaitRecord(**data)


def decision(**extra):
    data = dict(decision_id='d1', wait_id='w1', wait_version=1, scope='g', plan_ref='p',
                graph_ref='g', question_id='q', interrupt_id='i', actor='human', answer={'ok': True})
    data.update(extra)
    return DecisionEnvelope(**data)


def test_watchdog_expires_after_deadline_without_approving_disposition(repository):
    service = WaitService(repository)
    assert service.park(wait()) == 'waiting'
    repository.clock.advance(2000)
    assert service.watchdog() == ['w1']
    assert repository.rows[('waits', 'w1')]['body']['status'] == 'expired'
    dispositions = [row['body'] for (kind, _), row in repository.rows.items() if kind == 'outbox']
    assert any(row.get('kind') == 'resource_disposition' and row.get('approved') is False
               for row in dispositions)


def test_accept_requires_waiting_status_and_exact_scope_version(repository):
    service = WaitService(repository)
    service.park(wait())
    assert service.accept(decision()) == {'ok': True}
    assert service.accept(decision()) == {'ok': True}                     # idempotent by decision id
    # once ready, a second, different decision cannot re-open the question
    with pytest.raises(Conflict):
        service.accept(decision(decision_id='d2', answer={'ok': False}))
    # a wrong question/version against a fresh waiting record is stale
    service.park(wait(wait_id='w2'))
    with pytest.raises(Conflict):
        service.accept(decision(decision_id='d3', wait_id='w2', wait_version=2))


def test_watchdog_leaves_future_waits_untouched(repository):
    service = WaitService(repository)
    service.park(wait(deadline=10 ** 9))
    assert service.watchdog() == []
    assert repository.rows[('waits', 'w1')]['body']['status'] == 'waiting'
