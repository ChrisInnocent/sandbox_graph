"""Unique observed completions in explicitly bounded observation-clock windows."""
import math
from .common import number, union, duration, covered


def throughput(events,start,end,*,window_kind,clock_domain,coverage_intervals=None,active_intervals=None):
    number(start); number(end)
    if end < start or window_kind not in {'fixed_deadline','natural_drain'} or not clock_domain:
        raise ValueError('invalid observation window')
    span=end-start
    cover=union([(start,end)] if coverage_intervals is None else coverage_intervals,start,end)
    active=union([(start,end)] if active_intervals is None else active_intervals,start,end)
    unique={}
    late=set()
    for event in events:
        if event.get('clock_domain') != clock_domain:
            raise ValueError('unaligned completion clock domain')
        oid=event['operation_id']; t=number(event['completed_s'])
        if oid in unique and unique[oid] != t:
            raise ValueError('conflicting duplicate completion')
        unique[oid]=t
        if event.get('late'):
            late.add(oid)
    def in_window(t):
        return start <= t < end or (window_kind=='natural_drain' and start <= t == end)
    selected={oid:t for oid,t in unique.items() if in_window(t) and oid not in late}
    buckets=[]
    for k in range(math.ceil(span)):
        left,right=start+k,min(end,start+k+1)
        known=covered(cover,left,right)
        count=sum(left <= t < right or (window_kind=='natural_drain' and right==end and t==end) for t in selected.values())
        complete=math.isclose(known,right-left,rel_tol=1e-12,abs_tol=0)
        buckets.append({'start_s':left,'end_s':right,'duration_s':right-left,
                        'coverage':known/(right-left),'observed_count':count,
                        'count':count if complete else None,
                        'rate_per_s':count/(right-left) if complete else None})
    active_count=sum(any(a <= t < b or (window_kind=='natural_drain' and b==end and t==end) for a,b in active) for t in selected.values())
    active_span=duration(active)
    active_known=sum(covered(cover,a,b) for a,b in active)
    complete=span>0 and math.isclose(duration(cover),span,rel_tol=1e-12,abs_tol=0)
    active_complete=active_span>0 and math.isclose(active_known,active_span,rel_tol=1e-12,abs_tol=0)
    return {'formula_version':'throughput.observed.v1','window_kind':window_kind,'clock_domain':clock_domain,
            'buckets':buckets,'observed_count':len(selected),'count':len(selected) if complete else None,
            'coverage':duration(cover)/span if span else 0,
            'overall_per_s':len(selected)/span if complete else None,
            'deadline_outside':[oid for oid,t in unique.items() if t==end and window_kind=='fixed_deadline'],
            'late_operations':sorted(late),
            'outside_operations':[oid for oid,t in unique.items() if not in_window(t)],
            'active_duration_s':active_span,'active_count':active_count if active_complete else None,
            'active_per_s':active_count/active_span if active_complete else None}
