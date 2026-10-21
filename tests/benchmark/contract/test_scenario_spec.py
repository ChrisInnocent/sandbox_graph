"""Task 38.2 / RT-44,RT-45,BCD-15,BCD-16: per-scenario selection validation + case samples.

The scenario spec is frozen in BCD-15/BCD-16; this module turns it into an executable
validator. Missing required metrics/conditions is a contract error; optional-not-selected
is reported as ``not_selected``; a default cartesian product is never assumed.
"""
import pytest

from sandbox_bench.contracts import CaseContract
from sandbox_bench.scenario_spec import spec_for, validate_selection
from tests.benchmark.scenarios.test_scenarios import case

PRODUCTS = ('kuasar', 'cubesandbox', 'agentenv')
METHODS = ('B00.TEMPLATE_BUILD', 'B00.SNAPSHOT_BUILD', 'B01.TEMPLATE', 'B01.SNAPSHOT.DISK',
           'B01.SNAPSHOT.MEMORY', 'B02.FULL', 'B02.PREPARED', 'B03.PAUSE', 'B04.MEMORY',
           'B04.DISK', 'B05.FULL', 'B05.PREPARED', 'B05.NATIVE', 'B06.RUNNING', 'B06.PAUSED',
           'B07.FIRST', 'B07.STEADY', 'B07.OUTPUT', 'B07.ERROR', 'B08.LOCAL', 'B08.PUBLISHED',
           'B09.PLATFORM', 'B09.IDLE', 'B09.PAUSED', 'B10.GROWTH_LIST', 'B10.GROWTH_READY',
           'B10.PROJECTION', 'B10.FIXED_REQUESTS.WINDOW', 'B10.FIXED_REQUESTS.BATCH', 'B10.SLO_VERIFY')


def declarations(method):
    spec = spec_for(method)
    metrics = sorted(spec['metrics_required'] | spec['metrics_optional'])
    conditions = sorted(spec['conditions_required'] | spec['conditions_optional'] | spec['conditions_any_all'])
    return metrics, conditions


# --- 独立 oracle：spec 数据本身 -------------------------------------------------

def test_spec_data_matches_literal_expectations():
    assert spec_for('B03.PAUSE')['metrics_required'] == frozenset({'T-01', 'T-02', 'R-05'})
    assert spec_for('B00.TEMPLATE_BUILD')['metrics_required'] == frozenset(
        {'B-01', 'B-04', 'B-06', 'R-01', 'R-02'})
    assert 'N-03' in spec_for('B00.TEMPLATE_BUILD')['conditions_required']
    assert 'C-01' not in spec_for('B00.TEMPLATE_BUILD')['conditions_optional']   # no concurrent build
    assert spec_for('B10.GROWTH_LIST')['metrics_required'] >= frozenset({'C-01', 'C-02', 'C-03', 'H-05'})


# --- 三产品 × 方法：样例声明 R∪O 必通过 -----------------------------------------

@pytest.mark.parametrize('method', METHODS)
@pytest.mark.parametrize('product', PRODUCTS)
def test_sample_declares_all_required_and_passes(method, product):
    metrics, conditions = declarations(method)
    report = validate_selection(method, metrics, conditions)
    assert report['missing_metrics'] == [] and report['missing_conditions'] == []
    assert report['invalid_metrics'] == [] and report['invalid_conditions'] == []
    assert report['metrics_not_selected'] == []      # R∪O all declared -> nothing unselected


def test_case_v1_sample_with_declared_selection_is_accepted():
    metrics, conditions = declarations('B03.PAUSE')
    sample = case('B03.PAUSE', declared_metrics=tuple(metrics), declared_conditions=tuple(conditions))
    assert isinstance(sample, CaseContract)
    assert validate_selection('B03.PAUSE', sample.declared_metrics, sample.declared_conditions)['missing_metrics'] == []


# --- 负例：缺 R 集合同错误；O 集缺省标 not_selected ------------------------------

def test_missing_required_metric_is_a_contract_error():
    spec = spec_for('B03.PAUSE')
    metrics = [m for m in spec['metrics_required'] if m != 'T-02']
    with pytest.raises(ValueError, match='missing'):
        validate_selection('B03.PAUSE', metrics, sorted(spec['conditions_required']))


def test_declaring_a_metric_outside_the_scenario_is_rejected():
    spec = spec_for('B03.PAUSE')
    metrics = sorted(spec['metrics_required'] | {'C-02'})     # density metric not applicable
    with pytest.raises(ValueError, match='not applicable'):
        validate_selection('B03.PAUSE', metrics, sorted(spec['conditions_required']))


def test_optional_not_selected_is_reported_not_an_error():
    spec = spec_for('B03.PAUSE')
    report = validate_selection('B03.PAUSE', sorted(spec['metrics_required']),
                                sorted(spec['conditions_required']))
    assert report['metrics_not_selected']           # optional metrics remain unselected
    assert report['conditions_not_selected']


def test_either_concurrency_or_arrival_rate_satisfies_b01():
    spec = spec_for('B01.TEMPLATE')
    metrics = sorted(spec['metrics_required'])
    required = spec['conditions_required']
    validate_selection('B01.TEMPLATE', metrics, sorted(required | {'C-01'}))   # C-01 alone
    validate_selection('B01.TEMPLATE', metrics, sorted(required | {'C-02'}))   # C-02 alone
    with pytest.raises(ValueError, match='missing'):
        validate_selection('B01.TEMPLATE', metrics, sorted(required))          # neither C-01 nor C-02


def test_scenario_cli_validates_and_maps_contract_errors(tmp_path):
    import json
    import yaml
    from sandbox_bench.contracts import BackgroundConditions
    from sandbox_bench.runner import main
    metrics, conditions = declarations('B03.PAUSE')
    good = case('B03.PAUSE', declared_metrics=tuple(metrics), declared_conditions=tuple(conditions),
                background=BackgroundConditions(existing_count=0, instance_state='active', dirty_bytes=0))
    good_path = tmp_path / 'good.yaml'
    good_path.write_text(yaml.safe_dump(good.model_dump(mode='json')))
    out = tmp_path / 'out.json'
    assert main(['scenario', '--case', str(good_path), '--output', str(out)]) == 0
    assert json.loads(out.read_text())['status'] == 'valid'
    bad = case('B03.PAUSE', declared_metrics=('T-01',), declared_conditions=('N-01', 'N-04', 'SP-01', 'D-02'),
               background=BackgroundConditions(existing_count=0, instance_state='active', dirty_bytes=0))
    bad_path = tmp_path / 'bad.yaml'
    bad_path.write_text(yaml.safe_dump(bad.model_dump(mode='json')))
    assert main(['scenario', '--case', str(bad_path), '--output', str(out)]) == 1   # missing R set
