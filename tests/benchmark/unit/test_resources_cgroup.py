"""cgroup v2/v1 true memory-peak reading with frozen trees; no real cgroup required.

The kernel `memory.peak` is a true high-water mark, strictly stronger than a sampled PSS
maximum, but it is NOT PSS. oracle 为手写文件内容，不使用被测函数生成期望值。
"""
import pytest

from sandbox_bench.metrics import cgroup_true_peak
from sandbox_bench.resources import parse_cgroup_value, sample_cgroup


def reader(tree):
    def read(path):
        key = str(path)
        if key not in tree:
            raise FileNotFoundError(key)
        return tree[key]
    return read


V2 = {
    '/cg/sys/kernel/random/boot_id': 'boot-1\n',
    '/cg/sandbox/g1/cpu.stat': 'usage_usec 2500000\nuser_usec 1500000\nsystem_usec 1000000\n',
    '/cg/sandbox/g1/memory.peak': '104857600\n',
    '/cg/sandbox/g1/memory.current': '73400320\n',
}
V1 = {
    '/cg/sys/kernel/random/boot_id': 'boot-1\n',
    '/cg/sandbox/g1/cpu.stat': 'usage_usec 1000000\n',
    '/cg/sandbox/g1/memory.max_usage_in_bytes': '52428800\n',
    '/cg/sandbox/g1/memory.usage_in_bytes': '41943040\n',
}


def test_v2_peak_and_cpu_are_read_without_any_quota():
    sample = sample_cgroup('/cg', '/sandbox/g1', environment='env', node='node1', read_text=reader(V2))
    assert sample['status'] == 'valid'
    assert sample['memory_peak_bytes'] == 104857600
    assert sample['memory_current_bytes'] == 73400320
    assert sample['cpu_seconds'] == 2.5
    assert sample['identity'] == {'environment': 'env', 'node': 'node1', 'boot_id': 'boot-1', 'cgroup': '/sandbox/g1'}


def test_v1_fallback_reads_max_usage_in_bytes():
    sample = sample_cgroup('/cg', '/sandbox/g1', environment='env', node='node1', read_text=reader(V1))
    assert sample['status'] == 'valid'
    assert sample['memory_peak_bytes'] == 52428800
    assert sample['cpu_seconds'] == 1.0


def test_missing_peak_is_unavailable_never_zero():
    tree = {k: v for k, v in V2.items() if 'memory.peak' not in k}
    sample = sample_cgroup('/cg', '/sandbox/g1', environment='env', node='node1', read_text=reader(tree))
    assert sample['status'] == 'unavailable'
    assert sample['reason'] == 'cgroup_metric_missing'
    assert sample['memory_peak_bytes'] is None


def test_relative_path_and_bad_value_are_rejected():
    with pytest.raises(ValueError):
        sample_cgroup('/cg', 'sandbox/g1', environment='env', node='node1', read_text=reader(V2))
    with pytest.raises(ValueError):
        parse_cgroup_value('not-a-number\n', label='memory.peak')
    with pytest.raises(ValueError):
        parse_cgroup_value('1 2\n', label='memory.peak')


def test_true_peak_is_max_over_valid_samples_and_distinct_from_pss():
    samples = [
        {'status': 'valid', 'memory_peak_bytes': 100, 'clock_domain': 'n:b'},
        {'status': 'valid', 'memory_peak_bytes': 350, 'clock_domain': 'n:b'},
        {'status': 'unavailable', 'memory_peak_bytes': None, 'clock_domain': 'n:b'},
    ]
    result = cgroup_true_peak(samples)
    assert result['status'] == 'valid' and result['true_peak_bytes'] == 350
    assert result['sample_count'] == 2
    assert cgroup_true_peak([])['true_peak_bytes'] is None
    assert cgroup_true_peak([])['status'] == 'unavailable'


def test_sampled_cgroup_feeds_the_true_peak_metric():
    samples = [sample_cgroup('/cg', '/sandbox/g1', environment='env', node='node1', read_text=reader(V2)),
               sample_cgroup('/cg', '/sandbox/g1', environment='env', node='node1', read_text=reader(V1))]
    assert cgroup_true_peak(samples)['true_peak_bytes'] == 104857600


def test_analysis_reports_cgroup_peak_and_cpu_distinct_from_pss():
    from sandbox_bench.analysis import analyze_case
    records = {'case_id': 'c', 'operations': [], 'events': [],
               'window': {'start_s': 0, 'end_s': 2, 'window_kind': 'natural_drain', 'clock_domain': 'n:b'},
               'resource_series': [], 'cohorts': [], 'assertions': [], 'cleanup_ledger': [], 'raw_refs': [],
               'cgroup_series': [
                   {'status': 'valid', 'identity': {'cgroup': '/sb/1'}, 'memory_peak_bytes': 100, 'cpu_seconds': 1.0},
                   {'status': 'valid', 'identity': {'cgroup': '/sb/1'}, 'memory_peak_bytes': 300, 'cpu_seconds': 2.5},
                   {'status': 'unavailable', 'identity': {'cgroup': '/sb/2'}, 'memory_peak_bytes': None, 'cpu_seconds': None}]}
    resources = analyze_case(records)['resources']
    assert resources['cgroup']['true_peak_bytes'] == 300
    assert resources['cgroup']['cpu_seconds'] == 1.5
    assert resources['cgroup']['identities'] == 1
    assert 'not PSS' in resources['cgroup']['note']
