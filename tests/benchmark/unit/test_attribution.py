"""Attribution integrity oracle: every measured unit has an owner, or is isolated.

oracle 为手写身份/归属，不使用被测函数生成期望值。
"""
import json

import pytest

from sandbox_bench.analysis import analyze_case
from sandbox_bench.attribution import (attribution_report, attributed_series,
                                       identity_key, inventory_index)


def identity(pid, start=1):
    return {'environment': 'e', 'node': 'n', 'boot_id': 'b', 'pid': pid, 'start_ticks': start}


INVENTORY = [
    {'identity': identity(1), 'roles': ['management'], 'components': ['agent']},
    {'identity': identity(2), 'roles': ['work'], 'components': ['vmm'], 'sandbox_id': 's1', 'generation': 'g1'},
]


def process(pid, pss, cpu, start=1):
    return {'identity': identity(pid, start), 'pss_bytes': pss, 'cpu_seconds': cpu,
            'roles': [], 'components': []}


def sample(t, processes):
    return {'kind': 'resource_sample', 't_s': t, 'clock_domain': 'n:b',
            'inventory_complete': True, 'processes': processes}


def records():
    return {'case_id': 'c',
            'operations': [{'operation_id': '0', 'status': 'success', 'elapsed_s': 1}],
            'events': [], 'window': {'start_s': 0, 'end_s': 2, 'window_kind': 'natural_drain', 'clock_domain': 'n:b'},
            'resource_max_gap_s': 2, 'inventory': INVENTORY,
            'resource_series': [sample(0, [process(1, 10, 0), process(2, 20, 0)]),
                                sample(1, [process(1, 10, .5), process(2, 20, 1), process(99, 999, 9)]),
                                sample(2, [process(1, 10, 1), process(2, 20, 2)])],
            'cohorts': [], 'assertions': [], 'cleanup_ledger': [], 'raw_refs': []}


def test_identity_requires_full_process_tuple():
    with pytest.raises(ValueError):
        identity_key({'pid': 1})
    assert identity_key(identity(7))[3] == 7


def test_inventory_index_rejects_ownerless_and_reused_pids():
    with pytest.raises(ValueError):
        inventory_index([{'identity': identity(1), 'roles': ['management'], 'components': []}])
    with pytest.raises(ValueError):
        inventory_index([{'identity': identity(1, start=1), 'roles': ['m'], 'components': ['agent']},
                         {'identity': identity(1, start=2), 'roles': ['m'], 'components': ['agent']}])
    index = inventory_index(INVENTORY)
    assert set(index) == {identity_key(identity(1)), identity_key(identity(2))}


def test_unattributed_process_is_isolated_with_inventory_classes_injected():
    index = inventory_index(INVENTORY)
    filtered, dropped, reused = attributed_series(
        [sample(0, [process(1, 10, 0)]), sample(1, [process(99, 999, 9)])], index)
    assert [row['reason'] for row in dropped] == ['identity_not_in_inventory']
    assert reused == []
    assert filtered[0]['processes'][0]['components'] == ['agent']
    assert filtered[1]['processes'] == [] and filtered[1]['inventory_complete'] is False


def test_pid_reuse_is_detected_as_partial():
    index = inventory_index(INVENTORY)
    _, _, reused = attributed_series([sample(0, [process(2, 20, 0, start=1)]),
                                      sample(1, [process(2, 20, 1, start=2)])], index)
    assert reused == [2]
    report = attribution_report([sample(0, [process(2, 20, 0, start=1)]),
                                 sample(1, [process(2, 20, 1, start=2)])], INVENTORY)
    assert report['status'] == 'partial' and report['pid_reuse'] == [2]


def test_report_exposes_expected_observed_and_missing_components():
    report = attribution_report(records()['resource_series'], INVENTORY)
    assert report['status'] == 'partial' and report['unattributed_rows'] == 1
    assert report['expected_components'] == ['agent', 'vmm']
    assert report['observed_components'] == ['agent', 'vmm']


def test_analyze_case_excludes_ownerless_rows_and_flags_partial():
    records_data = records()
    records_data['inventory'].append({'identity': identity(3), 'roles': ['work'], 'components': ['sidecar']})
    result = analyze_case(records_data)
    attribution = result['resources']['attribution']
    assert attribution['status'] == 'partial' and attribution['unattributed_rows'] == 1
    # Declared-but-unobserved component lowers coverage instead of being silently dropped.
    assert attribution['missing_components'] == ['sidecar']
    # Component math only ever contains owner-bearing classes; pid 99 is nowhere.
    assert set(result['resources']['by_component']) <= {'agent', 'vmm', 'sidecar'}
    assert '999' not in json.dumps(result['resources']['by_component'])
    assert result['resources']['status'] == 'partial'
