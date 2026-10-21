"""RT-45 / BCD-16 §16.4 / BCT-12-COND: background-condition contract fields and validate_case."""
import pytest

from sandbox_bench.contracts import BackgroundConditions
from sandbox_bench.scenario_spec import condition_field, spec_for, validate_case, validate_conditions
from tests.benchmark.scenarios.test_scenarios import case


def test_condition_field_table_is_literal():
    assert condition_field('N-01') == 'background.existing_count'
    assert condition_field('N-03') == 'background.background_count'
    assert condition_field('C-01') == 'load.concurrency'
    assert condition_field('SP-01') == 'spec'
    assert condition_field('TL-03') == 'ttl_s'
    assert condition_field('D-04') == 'source.artifact_kind'
    with pytest.raises(ValueError):
        condition_field('ZZ-99')


def b03(**extra):
    spec = spec_for('B03.PAUSE')
    kwargs = dict(declared_metrics=tuple(spec['metrics_required']),
                  declared_conditions=tuple(spec['conditions_required']))
    kwargs.update(extra)
    return case('B03.PAUSE', **kwargs)


def test_missing_required_condition_field_is_a_contract_error():
    # B03 requires N-01,N-04,SP-01,D-02; D-02 (dirty_bytes) is not set.
    with pytest.raises(ValueError, match='missing'):
        validate_conditions(b03(background=BackgroundConditions(existing_count=0, instance_state='active')))


def test_all_required_condition_fields_pass_and_optional_is_not_selected():
    c = b03(background=BackgroundConditions(existing_count=0, instance_state='active', dirty_bytes=0))
    report = validate_conditions(c)
    assert report['missing'] == []
    assert report['not_selected']          # C-01, TL-01, TL-03 optional


def test_validate_case_runs_both_selection_and_conditions():
    c = b03(background=BackgroundConditions(existing_count=0, instance_state='active', dirty_bytes=0))
    report = validate_case(c)
    assert report['selection']['missing_metrics'] == []
    assert report['conditions']['missing'] == []


def test_validate_case_rejects_missing_metric_and_missing_condition_together():
    spec = spec_for('B03.PAUSE')
    c = case('B03.PAUSE', declared_metrics=('T-01',),           # missing T-02, R-05
             declared_conditions=tuple(spec['conditions_required']))
    with pytest.raises(ValueError):
        validate_case(c)
