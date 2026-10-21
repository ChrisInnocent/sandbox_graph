"""WF-37 / D19 / ORC-074,075: N09 ownership sweep catches orphans and verifies no residual.

The sweep runs when the environment declares a restore contract. Warm-pool sandboxes
(no run_id) and baseline ids are never deleted; a residual keeps cleanup incomplete.
"""
import pytest

from sandbox_bench.session import BenchSession
from tests.benchmark.scenarios.test_scenarios import case

CONFIG = {'product': 'agentenv', 'api_url': 'http://localhost:1', 'data_url': 'http://localhost:1',
          'restore': {'batch_size': 2, 'batch_gap_s': 0, 'max_rounds': 5}}


class FakeAdapter:
    def __init__(self, *, warm=(), delete_fails=()):
        self.owned = []
        self.warm = list(warm)
        self.delete_fails = set(delete_fails)
        self.deleted = []

    def register(self, sandbox_id):
        self.owned.append(sandbox_id)

    def list_owned(self, run_id, timeout):
        rows = [{'sandbox_id': sid, 'metadata': {'run_id': run_id},
                 'private': {'metadata': {'run_id': run_id}}} for sid in self.owned]
        rows += [{'sandbox_id': sid, 'metadata': {}, 'private': {'metadata': {}}} for sid in self.warm]
        return rows

    def delete(self, handle, timeout):
        sid = handle['sandbox_id']
        if sid in self.delete_fails:
            raise RuntimeError('delete failed')
        if sid in self.owned:
            self.owned.remove(sid)
        self.deleted.append(sid)


def session(tmp_path, adapter):
    bench = BenchSession(case('B01.TEMPLATE'), CONFIG, tmp_path)
    bench.adapter = adapter
    return bench


async def test_n09_sweep_deletes_orphans_and_excludes_warm_pool(tmp_path):
    adapter = FakeAdapter(warm=['warm-1'])
    bench = session(tmp_path, adapter)
    bench.journal.own('sandbox', 's1', {'environment': bench.environment})   # journal-owned
    adapter.register('s1')
    adapter.register('orphan-2')                                            # created, never journaled
    result = await bench._N09()
    assert set(adapter.deleted) == {'s1', 'orphan-2'}
    assert 'warm-1' not in adapter.deleted
    assert result['restore_sweep']['status'] == 'complete' and result['residual_ids'] == []
    assert result['status'] == 'completed'


async def test_n09_residual_keeps_cleanup_incomplete(tmp_path):
    adapter = FakeAdapter(delete_fails={'orphan-9'})
    bench = session(tmp_path, adapter)
    adapter.register('orphan-9')
    result = await bench._N09()
    assert result['status'] == 'incomplete' and 'orphan-9' in result['residual_ids']


async def test_no_restore_contract_skips_the_sweep(tmp_path):
    bench = BenchSession(case('B01.TEMPLATE'),
                         {k: v for k, v in CONFIG.items() if k != 'restore'}, tmp_path)
    bench.adapter = FakeAdapter()
    result = await bench._N09()
    assert result['restore_sweep'] is None and result['status'] == 'completed'


def test_incomplete_cleanup_maps_to_a_partial_archive():
    from sandbox_graph.domain.models import NodeResult
    from sandbox_graph.services.closure import ClosureService
    incomplete = NodeResult(execution_status='succeeded', scope_disposition='fulfilled',
                            data_status='valid', cleanup_status='incomplete')
    assert ClosureService.classify_scope(incomplete, 'p') == 'partial'
