"""BCT-08: each listed mutation must be caught by an explicit offline assertion.

These are adversarial inverse cases: the mutated (wrong) behaviour is fed in and the
production invariant must reject or exclude it. No mutation may silently pass.
"""
import pytest

from sandbox_bench.attribution import identity_key
from sandbox_bench.measure import summarize
from sandbox_bench.metrics.latency import summarize_latency
from sandbox_bench.metrics.resources import cohort_pss, cpu_summary, resource_mean
from sandbox_bench.resources import guest_net_pss


def _interval(a, b, cores, component):
    return {'start_s': a, 'end_s': b, 'cores': cores, 'component': component, 'clock_domain': 'n:b'}


def test_component_peaks_are_never_summed():
    intervals = [_interval(0, 1, 8, 'A'), _interval(1, 2, 1, 'A'),
                 _interval(0, 1, 1, 'B'), _interval(1, 2, 8, 'B')]
    result = cpu_summary(intervals, 0, 2, expected_components={'A', 'B'})
    assert result['sampled_peak_cores'] == 9          # not 8+8=16
    assert result['component_peaks'] == {'A': 8, 'B': 8}


def test_missing_sample_is_never_zero():
    samples = [{'t_s': 0, 'value': 10, 'clock_domain': 'n:b', 'cohort': ['s'], 'segment_id': 0},
               {'t_s': 5, 'value': 10, 'clock_domain': 'n:b', 'cohort': ['s'], 'segment_id': 0}]
    result = resource_mean(samples, 0, 5, max_gap_s=1)
    assert result['mean'] is None and result['coverage'] == 0


def test_start_ticks_are_required_for_process_identity():
    with pytest.raises(ValueError):
        identity_key({'environment': 'e', 'node': 'n', 'boot_id': 'b', 'pid': 1})


def test_timeout_is_not_a_success_latency():
    rows = [{'status': 'success', 'elapsed_s': 1, 'operation_id': 'a',
             'start_clock_domain': 'n:b', 'end_clock_domain': 'n:b'},
            {'status': 'timeout', 'elapsed_s': 99, 'operation_id': 'b',
             'start_clock_domain': 'n:b', 'end_clock_domain': 'n:b'}]
    result = summarize_latency(rows)
    assert result['n_success'] == 1 and result['failures'] == {'timeout': 1}
    assert result['latency_s']['max'] == 1


def test_requested_count_is_not_the_actual_live_cohort():
    result = cohort_pss(['s1', 's2', 's3'], {'s1': 10})  # only one of three observed
    assert result['direct_mean_bytes'] is None
    assert result['coverage'] == pytest.approx(1 / 3)


def test_guest_deduction_needs_a_definition_not_a_config_number():
    assert guest_net_pss(1000, {'deduction_bytes': 500}, domain='d', window='w')['status'] == 'unavailable'
    proof = {'definition': 'guest-os', 'method': 'smaps-rollup', 'version': '1', 'source': 'audit',
             'deduction_bytes': 500, 'domain': 'd', 'window': 'w', 'coverage': 1}
    ok = guest_net_pss(1000, proof, domain='d', window='w')
    assert ok['status'] == 'valid' and ok['net_pss_bytes'] == 500


def test_summary_excludes_failures_from_success_throughput():
    rows = [{'status': 'success', 'elapsed_s': 1}, {'status': 'failed', 'elapsed_s': 2},
            {'status': 'timeout', 'elapsed_s': 3}]
    result = summarize(rows, 10)
    assert result['success_count'] == 1 and result['failed_count'] == 2
    assert result['successful_throughput_per_s'] == pytest.approx(0.1)
