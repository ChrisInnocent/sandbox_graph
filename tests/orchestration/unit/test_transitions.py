"""D18.1 / ORC-016-019,050,051: separated execution state, CAS and bounded retry."""
import pytest

from sandbox_graph.domain.models import NodeResult
from sandbox_graph.domain.transitions import merge_revision, retry_allowed, transition


def result(status='pending', **extra):
    return NodeResult.model_validate({'execution_status': status, **extra})


def test_terminal_execution_states_are_never_revived():
    for terminal in ('succeeded', 'failed', 'cancelled'):
        with pytest.raises(ValueError, match='new attempt'):
            transition(result(terminal), 'running', 1, 1)


def test_cas_revision_mismatch_is_a_version_conflict():
    with pytest.raises(ValueError, match='VERSION_CONFLICT'):
        transition(result('pending'), 'running', expected_revision=1, revision=2)


def test_transition_does_not_touch_the_separated_dimensions():
    started = transition(result('pending'), 'running', 1, 1)
    assert started.execution_status == 'running'
    # A later review/perf/cleanup verdict must not be implied by execution success.
    assert (started.data_status, started.review_status, started.performance_status,
            started.cleanup_status) == ('unverified', 'pending', 'not_evaluated', 'not_needed')


def test_merge_revision_is_idempotent_conflicts_and_never_regresses():
    current = (2, {'a': 1})
    assert merge_revision(current, (2, {'a': 1})) == current          # same version, same content
    with pytest.raises(ValueError, match='conflicting'):
        merge_revision(current, (2, {'a': 2}))
    assert merge_revision(current, (1, {'a': 9})) == current          # old version cannot overwrite


def test_retry_only_for_transient_read_within_budget_and_deadline():
    assert retry_allowed('TRANSIENT_READ', attempts=1, max_attempts=3, now=0, deadline=10)
    assert not retry_allowed('TRANSIENT_READ', attempts=3, max_attempts=3, now=0, deadline=10)
    assert not retry_allowed('TRANSIENT_READ', attempts=1, max_attempts=3, now=11, deadline=10)
    assert not retry_allowed('PERMANENT', attempts=1, max_attempts=3, now=0, deadline=10)
    # A performance miss is a verdict, never a retry trigger.
    assert not retry_allowed('performance_fail', attempts=0, max_attempts=3, now=0, deadline=10)
