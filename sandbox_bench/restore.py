"""Environment restore between tests: small-batch, multi-round deletion + residual check.

Only sandboxes owned by this run (``metadata.run_id == run_id``) are ever deleted, so
warm-pool / idle / pre-existing baseline sandboxes are never touched. Deletion is
batched and spaced so a large cohort cannot stampede the node, and a residual sweep
re-lists afterwards because deletion is asynchronous on some products (Kuasar goes
``deleting`` -> ``dead``). A non-empty residual set is ``incomplete`` -- never success.
"""
from __future__ import annotations

import time


def select_batch(owned_ids, *, baseline_ids=(), batch_size=1):
    """Owned, non-baseline ids limited to one batch; baseline ids are never deleted."""
    if isinstance(batch_size, bool) or not isinstance(batch_size, int) or batch_size < 1:
        raise ValueError('positive integer batch size required')
    baseline = set(baseline_ids)
    ordered = [sid for sid in owned_ids if sid not in baseline]
    return ordered[:batch_size]


def owned_by_run(handle, run_id):
    """Defensive ownership check: the top-level row or the product ``private`` payload."""
    metadata = handle.get('metadata') or (handle.get('private') or {}).get('metadata') or {}
    return isinstance(metadata, dict) and metadata.get('run_id') == run_id


def restore_environment(adapter, *, run_id, batch_size, batch_gap_s, max_rounds, timeout,
                        deadline, baseline_ids=(), sleep=time.sleep, clock=time.monotonic):
    """Delete this run's sandboxes in small batches over multiple rounds, then verify residuals."""
    if not run_id:
        raise ValueError('run id required')
    if batch_size < 1 or max_rounds < 1 or timeout <= 0 or batch_gap_s < 0:
        raise ValueError('positive batch/round/timeout and non-negative gap required')
    if deadline <= clock():
        raise ValueError('restore deadline already expired')
    baseline = set(baseline_ids)
    deleted = []
    rounds = []

    def remaining_ids():
        remaining = max(0.001, deadline - clock())
        rows = adapter.list_owned(run_id, min(timeout, remaining))
        return [row['sandbox_id'] for row in rows
                if row['sandbox_id'] not in baseline and owned_by_run(row, run_id)]

    for index in range(max_rounds):
        ids = remaining_ids()
        if not ids:
            break
        batch = ids[:batch_size]
        failures = []
        for sandbox_id in batch:
            try:
                adapter.delete({'sandbox_id': sandbox_id}, min(timeout, max(0.001, deadline - clock())))
                deleted.append(sandbox_id)
            except Exception as exc:                       # a failed delete is a residual, not a success
                failures.append({'sandbox_id': sandbox_id, 'error_type': type(exc).__name__})
        rounds.append({'round': index, 'attempted': len(batch), 'deleted': len(batch) - len(failures),
                       'failures': failures, 'remaining_before': len(ids)})
        if clock() >= deadline:
            break
        sleep(batch_gap_s)

    residuals = list(dict.fromkeys(remaining_ids()))      # async deletes may need to settle
    return {'run_id': run_id, 'deleted': deleted, 'rounds': rounds, 'residual_ids': residuals,
            'status': 'complete' if not residuals else 'incomplete'}
