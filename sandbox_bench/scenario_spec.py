"""Per-scenario metric/condition selection spec (RT-44/RT-45, BCD-15/BCD-16).

Turns the frozen matrices into an executable validator: required metrics/conditions are
mandatory (missing -> contract error), a metric/condition outside the scenario is
rejected, and optional-not-selected is reported as ``not_selected``. A default cartesian
product is never assumed.
"""
from __future__ import annotations

from functools import lru_cache


def _expand(tokens):
    out = set()
    for token in tokens:
        if '..' in token:
            start, end = token.split('..')
            prefix_a, num_a = start.rsplit('-', 1)
            prefix_b, num_b = end.rsplit('-', 1)
            if prefix_a != prefix_b:
                raise ValueError(f'range prefixes differ: {token}')
            out.update(f'{prefix_a}-{n:02d}' for n in range(int(num_a), int(num_b) + 1))
        else:
            out.add(token)
    return frozenset(out)


def _family(mr, mo, cr, co, any_groups=()):
    any_all = frozenset(m for group in any_groups for m in group)
    return {'metrics_required': _expand(mr), 'metrics_optional': _expand(mo),
            'conditions_required': _expand(cr), 'conditions_optional': _expand(co),
            'conditions_any': tuple(frozenset(g) for g in any_groups),
            'conditions_any_all': any_all}


# Frozen from BCD-15 / BCD-16.
_FAMILIES = {
    'B00': _family(['B-01', 'B-04', 'B-06', 'R-01', 'R-02'],
                   ['T-03', 'T-04', 'S-01..S-05', 'B-02', 'B-03', 'B-05', 'A-01', 'A-02'],
                   ['N-03', 'W-02', 'SP-01', 'D-01', 'D-04'],
                   ['D-02', 'D-03', 'W-01', 'TL-01', 'TL-02', 'EP-01']),
    'B01': _family(['T-01', 'T-02', 'T-05', 'H-03', 'R-01..R-06'],
                   ['T-03', 'H-01', 'H-02', 'S-01..S-07', 'A-01', 'A-05'],
                   ['N-01', 'SP-01', 'D-04', 'W-02', 'EP-01', 'EP-02'],
                   ['C-03', 'C-04', 'C-05', 'SP-02', 'SP-03', 'W-01', 'TL-01..TL-04', 'DEP-01', 'N-04'],
                   [('C-01', 'C-02')]),
    'B02': _family(['T-01', 'T-02', 'T-04', 'H-03', 'R-01..R-06', 'A-02'],
                   ['T-03', 'T-05', 'B-01', 'B-04', 'S-01..S-07'],
                   ['N-01', 'SP-01', 'D-01', 'D-04', 'W-02', 'EP-01'],
                   ['C-03', 'C-04', 'C-05', 'D-02', 'D-03', 'W-01', 'TL-01..TL-04', 'DEP-01', 'N-04'],
                   [('C-01', 'C-02')]),
    'B03': _family(['T-01', 'T-02', 'R-05'], ['H-03', 'S-01..S-05', 'A-03'],
                   ['N-01', 'N-04', 'SP-01', 'D-02'], ['C-01', 'TL-01', 'TL-03']),
    'B04': _family(['T-01', 'T-02', 'T-05', 'R-05', 'R-06'], ['H-03', 'S-01..S-05', 'A-05'],
                   ['N-01', 'N-04', 'SP-01', 'D-02', 'D-03', 'EP-02'], ['W-02', 'TL-01']),
    'B05': _family(['T-01', 'T-02', 'H-03', 'R-01..R-06'], ['S-01..S-07', 'A-03', 'A-04'],
                   ['N-01', 'C-01', 'C-05', 'SP-01', 'D-02', 'D-03'],
                   ['C-03', 'C-04', 'TL-01', 'TL-02', 'W-02']),
    'B06': _family(['T-01', 'T-02', 'R-05', 'A-03', 'A-04'], ['H-03', 'S-01..S-05'],
                   ['N-01', 'N-04', 'SP-01'], ['C-01', 'TL-01']),
    'B07': _family(['T-01', 'T-02', 'R-06', 'R-02'], ['T-05', 'H-03', 'S-01..S-05'],
                   ['N-01', 'SP-01', 'L-01', 'EP-02'], ['C-01', 'TL-01', 'TL-02', 'W-02']),
    'B08': _family(['T-01', 'T-02', 'A-01', 'A-02', 'R-05', 'R-06'],
                   ['T-04', 'S-01..S-05', 'A-04', 'A-05'],
                   ['N-01', 'SP-01', 'D-02', 'D-03', 'W-02'], ['TL-01', 'TL-02', 'C-01', 'DEP-01']),
    'B09': _family(['S-04', 'S-05', 'S-06', 'R-05'], ['S-01', 'S-02', 'S-03', 'A-03', 'A-04', 'C-04'],
                   ['N-01', 'N-04', 'SP-01', 'DEP-01', 'TL-01'], ['W-02', 'TL-02', 'L-01']),
    'B10': _family(['C-01', 'C-02', 'C-03', 'H-05', 'S-04', 'S-05', 'S-06', 'R-01..R-05'],
                   ['H-01..H-04', 'C-04', 'A-04', 'S-01', 'S-02', 'S-03', 'S-07'],
                   ['N-01', 'N-02', 'C-01', 'C-03', 'C-05', 'SP-01', 'TL-01', 'TL-03', 'DEP-01'],
                   ['C-02', 'C-04', 'N-03', 'N-04', 'W-01', 'W-02', 'TL-02', 'TL-04', 'L-01']),
    'E01': _family(['T-01', 'T-02', 'H-03', 'S-01..S-05'], ['R-01..R-06', 'S-06', 'S-07'],
                   ['L-01', 'SP-01', 'N-01'], ['C-01', 'TL-01', 'W-02']),
    'E02': _family(['R-05', 'A-05'], ['T-01', 'S-01..S-05'],
                   ['D-02', 'D-03', 'SP-01', 'N-01'], ['TL-01']),
    'E03': _family(['T-01', 'T-02', 'H-03', 'S-01..S-05', 'C-05'], ['R-01..R-06', 'C-01..C-04'],
                   ['N-01', 'N-02', 'TL-01', 'SP-01'], ['C-01', 'L-01', 'TL-02']),
    'E04': _family(['S-01..S-05', 'C-01', 'C-02', 'C-03'],
                   ['T-01..T-05', 'H-01..H-05', 'R-01..R-06'],
                   ['C-01', 'N-02', 'SP-01'], ['C-02', 'C-03', 'C-04', 'TL-01']),
    'E05': _family(['S-01..S-05', 'T-01', 'T-02', 'H-03'], ['R-01..R-06', 'C-01..C-05'],
                   ['L-02', 'N-01', 'SP-01'], ['C-01', 'TL-01']),
    'E06': _family(['R-01..R-06', 'T-01', 'T-02'], ['H-03', 'S-01..S-05', 'A-04'],
                   ['L-01', 'TL-01', 'SP-01', 'N-01'], ['C-01', 'TL-02']),
}


def _family_key(method_id):
    if method_id.startswith('B00.'):
        return 'B00'
    head = method_id.split('.')[0]
    return head if head in _FAMILIES else None


@lru_cache(maxsize=None)
def spec_for(method_id):
    key = _family_key(method_id)
    if key is None:
        raise ValueError(f'unknown scenario {method_id!r}')
    return _FAMILIES[key]


# Condition ID -> dotted contract field (BCD-16 §16.4).
CONDITION_FIELDS = {
    'N-01': 'background.existing_count', 'N-02': 'background.target_count',
    'N-03': 'background.background_count', 'N-04': 'background.instance_state',
    'C-01': 'load.concurrency', 'C-02': 'load.arrival_rate', 'C-03': 'load.mode',
    'C-04': 'load.batch_size', 'C-05': 'load.requests', 'W-01': 'background.warm_pool_depth',
    'W-02': 'cache_profile', 'SP-01': 'spec', 'SP-02': 'background.spec_origin',
    'SP-03': 'background.effective_spec_ref', 'D-01': 'background.artifact_size_bytes',
    'D-02': 'background.dirty_bytes', 'D-03': 'background.snapshot_chain_depth',
    'D-04': 'source.artifact_kind', 'L-01': 'workload_ref', 'L-02': 'background.interference_ref',
    'TL-01': 'resources.window_s', 'TL-02': 'repeat_id', 'TL-03': 'ttl_s',
    'TL-04': 'background.warmup_s', 'EP-01': 'topology', 'EP-02': 'endpoint_kind',
}


def condition_field(condition_id):
    if condition_id not in CONDITION_FIELDS:
        raise ValueError(f'unknown condition {condition_id!r}')
    return CONDITION_FIELDS[condition_id]


def _present(case, path):
    value = case
    for segment in path.split('.'):
        value = getattr(value, segment, None)
        if value is None:
            return False
    return True


def validate_conditions(case):
    """Check that every required background condition has its contract field (RT-45)."""
    spec = spec_for(case.method_id)
    missing = [c for c in sorted(spec['conditions_required']) if not _present(case, condition_field(c))]
    for group in spec['conditions_any']:
        if not any(_present(case, condition_field(c)) for c in group):
            missing.append('|'.join(sorted(group)))
    declared = set(case.declared_conditions)
    not_selected = sorted(spec['conditions_optional'] - declared)
    if missing:
        raise ValueError(f'missing required condition fields for {case.method_id}: {missing}')
    return {'required': sorted(spec['conditions_required']), 'missing': [],
            'not_selected': not_selected, 'any_groups': [sorted(g) for g in spec['conditions_any']]}


def validate_case(case):
    """Combined per-scenario entry (task 37.3): selection + background conditions."""
    selection = validate_selection(case.method_id, case.declared_metrics, case.declared_conditions)
    conditions = validate_conditions(case)
    return {'method_id': case.method_id, 'selection': selection, 'conditions': conditions}


def validate_selection(method_id, declared_metrics, declared_conditions):
    spec = spec_for(method_id)
    declared_metrics, declared_conditions = set(declared_metrics), set(declared_conditions)
    allowed_metrics = spec['metrics_required'] | spec['metrics_optional']
    allowed_conditions = (spec['conditions_required'] | spec['conditions_optional']
                          | spec['conditions_any_all'])
    missing_metrics = sorted(spec['metrics_required'] - declared_metrics)
    invalid_metrics = sorted(declared_metrics - allowed_metrics)
    missing_conditions = sorted(spec['conditions_required'] - declared_conditions)
    invalid_conditions = sorted(declared_conditions - allowed_conditions)
    unsatisfied_any = [sorted(group) for group in spec['conditions_any']
                       if not (declared_conditions & group)]
    report = {'required_metrics': sorted(spec['metrics_required']),
              'declared_metrics': sorted(declared_metrics),
              'missing_metrics': missing_metrics, 'invalid_metrics': invalid_metrics,
              'metrics_not_selected': sorted(spec['metrics_optional'] - declared_metrics),
              'required_conditions': sorted(spec['conditions_required']),
              'missing_conditions': missing_conditions, 'invalid_conditions': invalid_conditions,
              'unsatisfied_any': unsatisfied_any,
              'conditions_not_selected': sorted(spec['conditions_optional'] - declared_conditions)}
    if invalid_metrics:
        raise ValueError(f'metric not applicable to {method_id}: {invalid_metrics}')
    if invalid_conditions:
        raise ValueError(f'condition not applicable to {method_id}: {invalid_conditions}')
    if missing_metrics:
        raise ValueError(f'missing required metrics for {method_id}: {missing_metrics}')
    if missing_conditions or unsatisfied_any:
        raise ValueError(f'missing required conditions for {method_id}: '
                         f'{missing_conditions} any={unsatisfied_any}')
    return report
