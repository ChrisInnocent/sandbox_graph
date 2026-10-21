"""Batched, ownership-scoped environment restore; warm/baseline sandboxes are never deleted."""
from types import SimpleNamespace

import pytest
import yaml

from sandbox_bench import runner
from sandbox_bench.restore import restore_environment, select_batch

pytestmark = pytest.mark.restore


class FakeClock:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value

    def sleep(self, seconds):
        self.value += seconds


class FakeList:
    """A node whose owned cohort shrinks as deletes land; warm/baseline rows are always listed."""

    def __init__(self, owned, *, warm=(), baseline=(), delete_fails=(), async_settle=0):
        self.owned = list(owned)
        self.warm = list(warm)
        self.baseline = list(baseline)
        self.delete_fails = set(delete_fails)
        self.async_settle = async_settle
        self.pending = []
        self.lists = 0
        self.deleted = []
        self.rounds_seen = []

    def list_owned(self, run_id, timeout):
        self.lists += 1
        rows = []

        def add(sid, metadata):
            if metadata.get('run_id') == run_id:               # adapter-level ownership filter
                rows.append({'sandbox_id': sid, 'metadata': metadata})

        for sid in self.owned:
            add(sid, {'run_id': run_id})
        for sid in self.baseline:
            add(sid, {'run_id': run_id})                       # same run, pre-existing baseline
        for sid in self.warm:
            add(sid, {})                                       # warm pool: no run_id, never listed
        if self.pending:
            self.rounds_seen.append(len(self.pending))
            if len(self.rounds_seen) > self.async_settle:
                self.pending = []
        for sid in self.pending:
            add(sid, {'run_id': run_id})
        return rows

    def delete(self, handle, timeout):
        sid = handle['sandbox_id']
        if sid in self.delete_fails:
            raise RuntimeError('delete failed')
        if sid in self.owned:                                  # idempotent: an already-deleted id is a no-op
            self.owned = [x for x in self.owned if x != sid]
            self.deleted.append(sid)
            if self.async_settle:
                self.pending.append(sid)


def restore(adapter, clock, **overrides):
    args = dict(run_id='run-1', batch_size=3, batch_gap_s=1, max_rounds=10, timeout=5,
                deadline=10_000, baseline_ids=adapter.baseline, sleep=clock.sleep, clock=clock)
    args.update(overrides)
    return restore_environment(adapter, **args)


def test_deletes_in_small_batches_over_multiple_rounds():
    adapter = FakeList(['s1', 's2', 's3', 's4', 's5', 's6', 's7'])
    report = restore(adapter, FakeClock())
    assert report['status'] == 'complete' and report['residual_ids'] == []
    assert [row['attempted'] for row in report['rounds']] == [3, 3, 1]   # never all at once
    assert adapter.deleted == ['s1', 's2', 's3', 's4', 's5', 's6', 's7']


def test_warm_pool_and_baseline_sandboxes_are_never_deleted():
    adapter = FakeList(['s1', 's2'], warm=['warm-1', 'warm-2'], baseline=['base-1'])
    report = restore(adapter, FakeClock())
    assert set(adapter.deleted) == {'s1', 's2'}
    assert 'warm-1' not in adapter.deleted and 'base-1' not in adapter.deleted
    assert report['status'] == 'complete'


def test_async_delete_that_has_not_settled_is_an_incomplete_residual():
    adapter = FakeList(['s1'], async_settle=99)
    report = restore(adapter, FakeClock(), max_rounds=2)
    assert report['status'] == 'incomplete'
    assert report['residual_ids'] == ['s1']


def test_a_failed_delete_is_reported_not_swallowed():
    adapter = FakeList(['s1', 's2', 's3', 's4'], delete_fails={'s2'})
    report = restore(adapter, FakeClock(), batch_size=2, max_rounds=2)
    failures = [f for row in report['rounds'] for f in row['failures']]
    assert {'sandbox_id': 's2', 'error_type': 'RuntimeError'} in failures
    assert report['status'] == 'incomplete' and 's2' in report['residual_ids']


def test_select_batch_is_ownership_and_baseline_scoped():
    assert select_batch(['a', 'b', 'c'], baseline_ids=['b'], batch_size=2) == ['a', 'c']
    with pytest.raises(ValueError):
        select_batch(['a'], batch_size=0)


def test_restore_kwargs_requires_run_id_batch_and_rounds():
    with pytest.raises(ValueError, match='missing'):
        runner.restore_kwargs({'batch_size': 1, 'max_rounds': 1})
    kwargs = runner.restore_kwargs({'run_id': 'r', 'batch_size': 2, 'max_rounds': 3})
    assert kwargs['run_id'] == 'r' and kwargs['batch_size'] == 2 and kwargs['max_rounds'] == 3


def test_restore_command_runs_the_batched_delete(tmp_path):
    config = {'product': 'kuasar', 'api_url': 'http://localhost:1', 'data_url': 'http://localhost:1',
              'run_id': 'r', 'batch_size': 2, 'max_rounds': 5, 'batch_gap_s': 0}
    cfg = tmp_path / 'restore.yaml'
    cfg.write_text(yaml.safe_dump(config))
    args = SimpleNamespace(config=str(cfg), output=str(tmp_path / 'out.json'))
    adapter = FakeList(['s1', 's2', 's3'])
    report = runner.plan_restore(args, adapter=adapter)
    assert report['status'] == 'complete' and adapter.deleted == ['s1', 's2', 's3']
    assert (tmp_path / 'out.json').exists()
