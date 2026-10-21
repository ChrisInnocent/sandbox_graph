"""Synthetic contracts: reject implicit scope/clock/status coercion (ORC-018/019/062)."""
import pytest
from pydantic import ValidationError
from sandbox_graph.domain.models import WorkKey, NodeResult, TimePoint, canonical_hash, Operation, ScriptRef
from sandbox_graph.domain.transitions import transition, merge_revision, retry_allowed


def test_identity_changes_for_independent_repeat_not_dictionary_order():
    a = WorkKey(group_id='G_A', scenario_id='B04', method_id='B04.MEMORY', method_version='1', condition_hash='a'*64, repeat_id=1)
    assert a.identity != a.model_copy(update={'repeat_id': 2}).identity
    assert canonical_hash({'a': 1, 'b': 2}) == canonical_hash({'b': 2, 'a': 1})
    with pytest.raises(ValidationError):
        WorkKey(**(a.model_dump() | {'secret': 'password'}))


def test_terminal_attempt_cannot_be_resurrected_or_performance_retried():
    result = NodeResult(execution_status='succeeded', data_status='valid', performance_status='fail')
    with pytest.raises(ValueError):
        transition(result, 'running', expected_revision=0, revision=0)
    assert not retry_allowed('PERFORMANCE_FAIL', attempts=0, max_attempts=3, now=1, deadline=10)
    assert not retry_allowed('EFFECT_UNKNOWN', attempts=0, max_attempts=3, now=1, deadline=10)
    assert result.execution_status == 'succeeded'
    assert result.review_status == 'pending'


def test_keyed_merge_preserves_latest_and_rejects_same_version_conflict():
    assert merge_revision((2, {'value': 3}), (1, {'value': 9})) == (2, {'value': 3})
    with pytest.raises(ValueError):
        merge_revision((2, {'value': 3}), (2, {'value': 9}))


def test_monotonic_clock_requires_process_identity_and_finite_values():
    with pytest.raises(ValidationError):
        TimePoint(value=1.0, clock='monotonic')
    with pytest.raises(ValidationError):
        TimePoint(value=float('nan'), clock='wall')
    with pytest.raises(ValueError):
        canonical_hash({'secret': float('nan')})


def test_work_key_condition_and_repeat_require_explicit_types():
    with pytest.raises(ValidationError):
        WorkKey(group_id='G_A',scenario_id='B01',method_id='B01.TEMPLATE',method_version='1',condition_hash='a'*64,repeat_id='1')
