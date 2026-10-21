"""Attribution integrity: no measured unit is ownerless.

Every process row must resolve to a known inventory identity *and* a declared
role/component class. Unknown identities are isolated into an explicit
``unattributed`` bucket -- never merged into a component -- so per-component math is
computed only from owner-bearing rows. The batch/offline path (``analyze_case`` and
``recompute_case``) re-derives the same report from frozen evidence, so a script can
recompute component attribution case-by-case without a live sampler.

Identity is the full process tuple ``(environment, node, boot_id, pid, start_ticks)``:
the pid alone is never sufficient, because a reused pid would silently merge two
different processes into one owner.
"""
from __future__ import annotations

import json

from .resources import deduplicate_inventory

IDENTITY_FIELDS = ('environment', 'node', 'boot_id', 'pid', 'start_ticks')
ATTRIBUTION_FORMULA = 'attribution.v1'


def identity_key(identity):
    if not isinstance(identity, dict) or any(field not in identity for field in IDENTITY_FIELDS):
        raise ValueError('complete process identity required')
    return tuple(identity[field] for field in IDENTITY_FIELDS)


def process_key(identity):
    """Canonical per-process label used by the CPU interval grouping."""
    return json.dumps(identity, sort_keys=True, separators=(',', ':'))


def expected_identity_keys(index):
    """Every declared identity must be present for full CPU coverage (fail-closed)."""
    return sorted({process_key(entry['identity']) for entry in index.values()})


def inventory_index(rows):
    """Declared identity -> class map; every row declares roles and components.

    Raises if a pid is bound to two identities (pid reuse) or a row has no class.
    """
    index = {}
    by_pid = {}
    for row in deduplicate_inventory(rows):
        identity = row.get('identity')
        key = identity_key(identity)
        roles = tuple(row.get('roles') or ())
        components = tuple(row.get('components') or ())
        if not roles or not components:
            raise ValueError('inventory row requires non-empty roles and components')
        pid = key[3]
        if pid in by_pid and by_pid[pid] != key:
            raise ValueError('pid maps to two identities; pid reuse is not attributable')
        by_pid[pid] = key
        index[key] = {'identity': identity, 'roles': roles, 'components': components,
                      'sandbox_id': row.get('sandbox_id'), 'generation': row.get('generation')}
    if not index:
        raise ValueError('non-empty inventory required')
    return index


def _sample_identity(process):
    identity = process.get('expected_identity') or process.get('identity')
    return identity_key(identity)


def attributed_series(series, index):
    """Split ``resource_sample`` processes into owner-bearing and unattributed rows.

    Unattributed rows are removed from the series (so component math never sees them)
    and returned with a reason; the enclosing snapshot is marked incomplete.
    """
    filtered = []
    dropped = []
    seen_pids = {}
    for sample in series:
        if sample.get('kind') != 'resource_sample':
            filtered.append(sample)
            continue
        keep = []
        for process in sample.get('processes', []):
            try:
                key = _sample_identity(process)
            except ValueError:
                dropped.append({'reason': 'missing_identity'})
                continue
            seen_pids.setdefault(key[3], set()).add(key)
            if key in index:
                owner = index[key]
                keep.append({**process, 'roles': list(owner['roles']), 'components': list(owner['components']),
                             'sandbox_id': owner['sandbox_id'], 'generation': owner['generation']})
            else:
                dropped.append({'reason': 'identity_not_in_inventory', 'identity': key})
        filtered.append({**sample, 'processes': keep,
                         'inventory_complete': bool(keep) and sample.get('inventory_complete', True)})
    reused = sorted(pid for pid, keys in seen_pids.items() if len(keys) > 1)
    return filtered, dropped, reused


def attribution_report(series, inventory):
    """Batch-safe provenance report for one case's resource evidence."""
    index = inventory_index(inventory)
    _, dropped, reused = attributed_series(series, index)
    expected = sorted({component for entry in index.values() for component in entry['components']})
    observed = set()
    for sample in series:
        if sample.get('kind') != 'resource_sample':
            continue
        for process in sample.get('processes', []):
            try:
                key = _sample_identity(process)
            except ValueError:
                continue
            entry = index.get(key)
            if entry:
                observed.update(entry['components'])
    reasons = sorted({row['reason'] for row in dropped})
    status = 'valid' if not dropped and not reused else 'partial'
    return {'formula_version': ATTRIBUTION_FORMULA, 'status': status,
            'inventory_identities': len(index),
            'unattributed_rows': len(dropped),
            'unattributed_reasons': reasons,
            'pid_reuse': reused,
            'expected_components': expected,
            'observed_components': sorted(observed),
            'missing_components': sorted(set(expected) - observed)}
