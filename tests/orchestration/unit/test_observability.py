"""P12.2 / ORC-021,023,069: structured trace, phase timing, usage availability, reuse.

Control-plane trace is separate from sandbox throughput; secrets and raw logs are
references only; missing usage is unknown, never zero.
"""
import pytest

from sandbox_bench.runner import redact
from sandbox_graph.domain.models import WorkInstance, WorkKey
from sandbox_graph.runtime.observability import ObservabilityService, TraceEvent, store_raw_log


def trace(**extra):
    data = dict(trace_ref='t1', run_ref='r', plan_ref='p', graph_ref='g', scope='s', node_id='N05',
                attempt_id='a', owner_epoch=1, phase='script', started_at=0.0, ended_at=1.0,
                status='succeeded', usage_status='unknown')
    data.update(extra)
    return TraceEvent(**data)


def test_secrets_are_redacted_but_references_and_ids_survive():
    redacted = redact({'sandbox_id': 's1', 'envdAccessToken': 'SECRET-TOKEN',
                       'api_key_ref': 'secret://vault/1', 'nested': {'password': 'p'}})
    assert redacted['envdAccessToken'] == '[REDACTED]'
    assert redacted['nested']['password'] == '[REDACTED]'
    assert redacted['api_key_ref'] == 'secret://vault/1'   # a reference is not a secret
    assert 'SECRET-TOKEN' not in str(redacted)


def test_redaction_recurses_through_lists():
    redacted = redact([{'private_key': 'k'}, {'ok': 1}])
    assert redacted[0]['private_key'] == '[REDACTED]' and redacted[1] == {'ok': 1}


def test_phase_recording_computes_duration_and_is_idempotent(repository):
    service = ObservabilityService(repository)
    ref = service.record(trace())
    assert service.record(trace()) == ref                       # same identity is idempotent
    summary = service.summary('r')
    assert summary['phases']['script'] == {'count': 1, 'duration_s': 1.0}
    assert summary['usage'] == {'known': 0, 'unknown': 1}


def test_negative_duration_and_fabricated_usage_are_rejected(repository):
    service = ObservabilityService(repository)
    with pytest.raises(ValueError, match='duration'):
        service.record(trace(ended_at=-1.0))
    with pytest.raises(ValueError, match='fabricated'):
        service.record(trace(usage_status='unknown', usage_tokens=5))
    with pytest.raises(ValueError, match='integer'):
        service.record(trace(usage_status='known', usage_tokens=None))


def test_raw_logs_and_secrets_are_references_only(repository):
    with pytest.raises(ValueError):
        store_raw_log('inline raw log text')
    assert store_raw_log('log://run/r/1') == 'log://run/r/1'
    with pytest.raises(ValueError):
        ObservabilityService(repository).record(trace(log_ref='inline text'))


def test_reuse_count_reflects_reused_work_instances(repository):
    key = WorkKey(group_id='g', scenario_id='s', method_id='B01.TEMPLATE', method_version='1',
                  condition_hash='a' * 64, repeat_id=1)
    with repository.transaction() as tx:
        tx.put('work_instances', 'w1', WorkInstance(
            work_id='w1', key=key, run_ref='r', plan_ref='p', environment_ref='e',
            reuse_from='old-delivery').model_dump(mode='json'))
    assert ObservabilityService(repository).summary('r')['reuse_count'] == 1
