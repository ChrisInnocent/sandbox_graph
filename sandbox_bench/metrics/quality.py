"""Failure classification and repeat strata; no implicit percentile averaging."""
from collections import Counter, defaultdict
from .latency import summarize_latency


def summarize_quality(rows, *, final_cohort=None):
    rows=list(rows)
    ids=[r['operation_id'] for r in rows]
    if len(set(ids))!=len(ids):
        raise ValueError('duplicate operation in quality records')
    cohort=None if final_cohort is None else sorted(set(final_cohort))
    accepted=sum(r.get('api_status') in {'success','accepted'} for r in rows)
    completed=sum(r['status']=='success' for r in rows)
    return {
        'formula_version':'quality.operations.v1',
        'n_attempts':len(rows),
        'api_success_count':accepted,
        'completed_success_count':completed,
        'accepted_rate':(accepted/len(rows)) if rows else None,
        'completed_success_rate':(completed/len(rows)) if rows else None,
        'unknown_count':sum(bool(r.get('unknown')) or r['status']=='unknown' for r in rows),
        'failure_statuses':dict(Counter(r['status'] for r in rows if r['status']!='success')),
        'failure_reasons':dict(Counter(r.get('reason') or r['status'] for r in rows if r['status']!='success')),
        'final_cohort':cohort,
        'final_cohort_count':None if cohort is None else len(cohort),
    }


def summarize_repeats(rows, *, pool_raw=False):
    rows=list(rows)
    strata=defaultdict(list)
    for row in rows:
        if 'repeat' not in row:
            raise ValueError('repeat identity required')
        strata[row['repeat']].append(row)
    result={'formula_version':'latency.repeats.v1',
            'repeats':{repeat:summarize_latency(group) for repeat,group in strata.items()}}
    if pool_raw:
        # Retain unique identity when local operation IDs are reused per repeat.
        pooled=[dict(row,operation_id=(row['repeat'],row.get('operation_id',i)),repeat=None)
                for i,row in enumerate(rows)]
        result['pooled']=summarize_latency(pooled)
    return result
