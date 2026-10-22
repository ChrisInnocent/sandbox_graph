"""Per-operation quantiles; callers must group repeats before pooling raw data."""
from collections import Counter
import math
from .common import number


def summarize_latency(rows, *, comparability_fields=('product','method','completion_kind','clock_domain','fixture_id','cache_mode','specification','repeat','caliber_version')):
    rows=list(rows)
    for key in comparability_fields:
        if any(row.get(key) != rows[0].get(key) for row in rows[1:]):
            raise ValueError(f'incomparable {key}')
    seen=set()
    for row in rows:
        if (row.get('start_clock_domain') != row.get('end_clock_domain')):
            raise ValueError('duration crosses clock epochs')
        oid=row.get('operation_id')
        if oid is not None and oid in seen:
            raise ValueError('duplicate operation in latency samples')
        seen.add(oid) if oid is not None else None
    values=sorted(number(r['elapsed_s'],'elapsed_s',nonnegative=True) for r in rows if r['status']=='success')
    result={'formula_version':'latency.linear.v1','n_success':len(values),'n_attempts':len(rows),
            'failures':dict(Counter(r['status'] for r in rows if r['status']!='success')),
            'comparability':{k:rows[0].get(k) for k in comparability_fields} if rows else {},
            'latency_s':None}
    if values:
        def quantile(q):
            p=(len(values)-1)*q
            lo,hi=math.floor(p),math.ceil(p)
            return values[lo]+(values[hi]-values[lo])*(p-lo)
        result['latency_s']={'mean':sum(values)/len(values),'min':values[0],'max':values[-1],
                             'p50':quantile(.5),'p95':quantile(.95),'p99':quantile(.99)}
    return result
