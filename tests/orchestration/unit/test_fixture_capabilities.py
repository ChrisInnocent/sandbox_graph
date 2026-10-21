"""P1.4 / T02: the synthetic fixtures can manufacture the required counter-examples."""
import pytest

from sandbox_bench.metrics.latency import summarize_latency
from sandbox_bench.metrics.quality import summarize_quality
from sandbox_bench.metrics.resources import resource_mean
from sandbox_graph.persistence.repository import Conflict
from sandbox_graph.runtime.leases import LeaseService


def test_fixture_manufactures_accepted_but_not_completed():
    quality = summarize_quality([{'operation_id': 'a', 'status': 'failed', 'api_status': 'success'}])
    assert quality['api_success_count'] == 1 and quality['completed_success_count'] == 0


def test_fixture_manufactures_a_cross_clock_sample():
    rows = [{'status': 'success', 'elapsed_s': 1, 'operation_id': 'a',
             'start_clock_domain': 'n:b', 'end_clock_domain': 'n:c'}]
    with pytest.raises(ValueError, match='clock'):
        summarize_latency(rows)


def test_fixture_manufactures_a_missing_sample():
    samples = [{'t_s': 0, 'value': 1, 'clock_domain': 'n:b', 'cohort': ['s'], 'segment_id': 0},
               {'t_s': 5, 'value': 1, 'clock_domain': 'n:b', 'cohort': ['s'], 'segment_id': 0}]
    assert resource_mean(samples, 0, 5, max_gap_s=1)['mean'] is None


def test_shared_host_domain_blocks_a_second_owner(repository, environments):
    assert repository.is_test_fixture is True     # memory UoW is explicitly not PostgreSQL
    service = LeaseService(repository)
    token = service.acquire_thread('t', 'owner', 100)
    service.acquire_all(('host_A',), scope='G_A', token=token)
    with pytest.raises(Conflict):
        service.acquire_all(('host_A',), scope='G_C', token=token)
