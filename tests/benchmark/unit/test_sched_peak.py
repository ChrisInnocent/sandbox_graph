"""Exact CPU-peak reconstruction from sched_switch, attributed per process class.

oracle 为手写字面量事件轨迹，不调用被测函数生成期望值。无 root/bcc 也能跑：这里
只测纯重建与峰值，不 attach eBPF。
"""
import pytest

from sandbox_bench.sched_peak import (BpfSchedSwitchCollector, attribution_from_inventory,
                                      peak_from_sched, reconstruct_on_cpu)

# Kuasar-style classes: management agent (100) vs. three sandbox VMM tasks (201-203).
ATTRIBUTION = {
    100: {'roles': ['management'], 'components': ['agent'], 'sandbox_id': None, 'generation': None},
    201: {'roles': ['work'], 'components': ['vmm'], 'sandbox_id': 's1', 'generation': 'g1'},
    202: {'roles': ['work'], 'components': ['vmm'], 'sandbox_id': 's2', 'generation': 'g1'},
    203: {'roles': ['work'], 'components': ['vmm'], 'sandbox_id': 's3', 'generation': 'g1'},
}

# Three VMM tasks run concurrently for only 1 microsecond; management runs later for 1us.
EVENTS = [
    {'t_ns': 0, 'prev_pid': 0, 'next_pid': 201},
    {'t_ns': 0, 'prev_pid': 0, 'next_pid': 202},
    {'t_ns': 0, 'prev_pid': 0, 'next_pid': 203},
    {'t_ns': 1_000, 'prev_pid': 201, 'next_pid': 0},
    {'t_ns': 1_000, 'prev_pid': 202, 'next_pid': 0},
    {'t_ns': 1_000, 'prev_pid': 203, 'next_pid': 0},
    {'t_ns': 20_000_000, 'prev_pid': 0, 'next_pid': 100},
    {'t_ns': 20_001_000, 'prev_pid': 100, 'next_pid': 0},
]
WINDOW = (0, 100_000_000)


def test_attribution_from_inventory_maps_roles_and_components():
    rows = [
        {'identity': {'pid': 100, 'boot_id': 'b', 'start_ticks': 1}, 'roles': ['management'], 'components': ['agent']},
        {'identity': {'pid': 201, 'boot_id': 'b', 'start_ticks': 1}, 'roles': ['work'], 'components': ['vmm'], 'sandbox_id': 's1', 'generation': 'g1'},
    ]
    attribution = attribution_from_inventory(rows)
    assert set(attribution) == {100, 201}
    assert attribution[100]['components'] == ['agent']
    assert attribution[201]['sandbox_id'] == 's1'
    assert attribution[100]['identity']['start_ticks'] == 1
    with pytest.raises(ValueError):
        attribution_from_inventory([])
    with pytest.raises(ValueError):
        attribution_from_inventory([{'identity': {'pid': 1, 'start_ticks': 1}, 'roles': ['w'], 'components': ['vmm']},
                                   {'identity': {'pid': 1, 'start_ticks': 2}, 'roles': ['w'], 'components': ['vmm']}])


def test_reconstruct_pairs_on_and_off_into_exact_intervals():
    recon = reconstruct_on_cpu(EVENTS, scope_pids={100, 201, 202, 203},
                               window_start_ns=WINDOW[0], window_end_ns=WINDOW[1])
    spans = sorted((i['pid'], i['start_ns'], i['end_ns']) for i in recon['intervals'])
    assert spans == [(100, 20_000_000, 20_001_000),
                     (201, 0, 1_000), (202, 0, 1_000), (203, 0, 1_000)]
    assert recon['unbounded_pids'] == [] and recon['gaps'] == []


def test_true_peak_catches_a_spike_coarse_sampling_misses():
    result = peak_from_sched(EVENTS, attribution=ATTRIBUTION, window_start_ns=WINDOW[0],
                             window_end_ns=WINDOW[1], clock_domain='bpf:ktime:boot-1')
    # Exact concurrency peak: three VMM tasks on-CPU at once.
    assert result['true_peak_cores'] == 3.0
    assert result['status'] == 'valid'
    # Views are per-class, not additive; total is the union over scoped tasks.
    assert result['true_peak_cores_by_role'] == {'management': 1.0, 'work': 3.0}
    assert result['true_peak_cores_by_component'] == {'agent': 1.0, 'vmm': 3.0}
    # A 100 ms cumulative-counter sample would average the 1 us x3 burst to ~4e-5 cores.
    sampled_equivalent = (3 * 1_000 + 1_000) / 1e9 / 0.1
    assert sampled_equivalent < 0.001 < result['true_peak_cores']


def test_task_already_on_cpu_at_window_start_is_clipped_and_partial():
    orphan = [{'t_ns': 5_000_000, 'prev_pid': 204, 'next_pid': 0}]
    attribution = {**ATTRIBUTION, 204: {'roles': ['work'], 'components': ['vmm'],
                                        'sandbox_id': 's4', 'generation': 'g1'}}
    result = peak_from_sched(orphan, attribution=attribution, window_start_ns=0,
                             window_end_ns=10_000_000, clock_domain='bpf:ktime:boot-1')
    assert result['status'] == 'partial'
    assert result['unbounded_pids'] == [204]
    assert result['true_peak_cores'] == 1.0


def test_empty_or_invalid_inputs_are_rejected():
    with pytest.raises(ValueError):
        peak_from_sched(EVENTS, attribution={}, window_start_ns=0, window_end_ns=1, clock_domain='c')
    with pytest.raises(ValueError):
        peak_from_sched(EVENTS, attribution=ATTRIBUTION, window_start_ns=1, window_end_ns=0, clock_domain='c')
    with pytest.raises(ValueError):
        peak_from_sched(EVENTS, attribution=ATTRIBUTION, window_start_ns=0, window_end_ns=1, clock_domain='')


def test_collector_reports_unavailable_without_faking_a_peak():
    status = BpfSchedSwitchCollector.availability()
    assert status['available'] is False
    assert status['reason'] in {'requires_root_or_cap_bpf', 'clang_missing', 'bcc_not_installed', 'tracefs_unavailable'}
    collector = BpfSchedSwitchCollector(run_id='run', clock_domain='bpf:ktime:boot-1')
    with pytest.raises(RuntimeError):
        collector.start()
    with pytest.raises(ValueError):
        BpfSchedSwitchCollector(run_id='', clock_domain='c')
