"""Resource integrals over covered intervals and identity-preserving cohorts."""
from collections import defaultdict
import math
from .common import number


def cpu_intervals(samples):
    """Compatibility list view of valid intervals; cpu_segments retains gaps."""
    return cpu_segments(samples)['intervals']


def cpu_segments(samples):
    """Pair adjacent samples per component; missing/reset/restart breaks a segment.

    Never stitch across a missing value, identity, clock epoch, or counter reset.
    """
    previous={}
    result=[]
    gaps=[]
    for row in sorted(samples,key=lambda r:r['t_s']):
        number(row['t_s'])
        component=row.get('component','process')
        if row.get('cpu_seconds') is not None:
            number(row['cpu_seconds'],nonnegative=True)
        old=previous.get(component)
        previous[component]=row
        if old is None:
            continue
        reason=None
        if row.get('identity') is None or old.get('identity') != row['identity']:
            reason='identity_changed'
        elif not row.get('clock_domain') or old.get('clock_domain') != row['clock_domain']:
            reason='clock_domain_changed'
        elif old.get('segment_id')!=row.get('segment_id'):
            reason='sample_gap'
        elif old.get('cpu_seconds') is None or row.get('cpu_seconds') is None:
            reason='missing_endpoint'
        if reason:
            gaps.append({'start_s':old['t_s'],'end_s':row['t_s'],'component':component,'reason':reason})
            continue
        elapsed=row['t_s']-old['t_s']
        delta=number(row['cpu_seconds'],nonnegative=True)-number(old['cpu_seconds'],nonnegative=True)
        if elapsed <= 0 or delta < 0:
            gaps.append({'start_s':old['t_s'],'end_s':row['t_s'],'component':component,
                         'reason':'counter_reset' if delta<0 else 'nonpositive_interval'})
            continue
        result.append({'start_s':old['t_s'],'end_s':row['t_s'],'cpu_seconds':delta,
                       'cores':delta/elapsed,'percent_one_core':100*delta/elapsed,
                       'component':component,'identity':row['identity'],'clock_domain':row['clock_domain']})
    return {'formula_version':'cpu.segments.v1','intervals':result,'gaps':gaps}


def cpu_summary(intervals,start,end,*,host_cores=None,expected_components=None):
    number(start); number(end)
    if end <= start:
        return {'formula_version':'cpu.interval.v1','status':'unavailable','reason':'zero_or_reversed_window',
                'cpu_seconds':None,'mean_cores':None,'sampled_peak_cores':None,'coverage':0}
    grouped=defaultdict(list)
    peaks={}
    component_seconds=defaultdict(float)
    for row in intervals:
        number(row['cores'],nonnegative=True)
        if row['end_s'] <= row['start_s'] or not row.get('clock_domain'):
            raise ValueError('invalid CPU interval')
        a,b=max(start,row['start_s']),min(end,row['end_s'])
        if b <= a:
            continue
        key=(row['clock_domain'],row.get('component','process'))
        grouped[key].append((a,b,row['cores']))
        label=row.get('component','process')
        peaks[label]=max(peaks.get(label,0),row['cores'])
        component_seconds[label]+=row['cores']*(b-a)
    for values in grouped.values():
        values.sort()
        if any(values[i][0] < values[i-1][1] for i in range(1,len(values))):
            raise ValueError('overlapping intervals for one component')
    domains={key[0] for key in grouped}
    result={'formula_version':'cpu.interval.v1','component_peaks':peaks,'cpu_seconds':None,
            'component_cpu_seconds':dict(component_seconds),'aligned_cpu_seconds':None,
            'mean_cores':None,'covered_mean_cores':None,'sampled_peak_cores':None,'coverage':0,
            'percent_one_core':None,'host_percent':None}
    if len(domains)>1:
        return dict(result,status='unavailable',reason='unaligned_clock_domains',
                    domains={domain:cpu_summary([r for r in intervals if r['clock_domain']==domain],start,end,host_cores=host_cores) for domain in sorted(domains)})
    expected=set(expected_components) if expected_components is not None else {key[1] for key in grouped}
    if {key[1] for key in grouped}-expected:
        raise ValueError('interval component outside expected inventory')
    edges=sorted({start,end}|{x for rows in grouped.values() for a,b,_ in rows for x in (a,b)})
    covered_s=integral=0
    total_peak=None
    for a,b in zip(edges,edges[1:]):
        contributions=[]
        for values in grouped.values():
            matching=[v for left,right,v in values if left<=a and right>=b]
            if matching:
                contributions.append(matching[0])
        if not grouped or len(contributions)!=len(expected):
            continue
        cores=sum(contributions)
        covered_s+=b-a
        integral+=cores*(b-a)
        total_peak=cores if total_peak is None else max(total_peak,cores)
    complete=math.isclose(covered_s,end-start,rel_tol=1e-12,abs_tol=0)
    result.update(status='valid' if complete else 'partial',
                  cpu_seconds=sum(component_seconds.values()) if grouped else None,
                  aligned_cpu_seconds=integral if covered_s else None,
                  mean_cores=integral/(end-start) if complete else None,
                  covered_mean_cores=integral/covered_s if covered_s else None,
                  sampled_peak_cores=total_peak,coverage=covered_s/(end-start))
    if complete:
        result['percent_one_core']=100*result['mean_cores']
        if host_cores is not None:
            number(host_cores,nonnegative=True)
            if host_cores==0:
                raise ValueError('host_cores must be positive')
            result['host_percent']=result['percent_one_core']/host_cores
    return result


def resource_mean(samples,start,end,*,max_gap_s):
    """Left-constant integral; no extension after the final evidence point."""
    number(start); number(end); number(max_gap_s,nonnegative=True)
    if end <= start or max_gap_s <= 0:
        raise ValueError('positive window and max gap required')
    rows=sorted(samples,key=lambda r:r['t_s'])
    integral=covered_s=0
    domains=set()
    for old,new in zip(rows,rows[1:]):
        dt=number(new['t_s'])-number(old['t_s'])
        if dt<=0:
            raise ValueError('resource timestamps must strictly increase')
        if (dt>max_gap_s or old.get('value') is None or new.get('value') is None
                or not old.get('clock_domain') or old.get('clock_domain')!=new.get('clock_domain')
                or old.get('segment_id')!=new.get('segment_id')
                or old.get('cohort')!=new.get('cohort')):
            continue
        a,b=max(start,old['t_s']),min(end,new['t_s'])
        if b>a:
            domains.add(old['clock_domain'])
            integral+=number(old['value'],nonnegative=True)*(b-a)
            covered_s+=b-a
    complete=math.isclose(covered_s,end-start,rel_tol=1e-12,abs_tol=0) and len(domains)==1
    if len(domains)>1:
        return {'formula_version':'resource.left_constant.v1','mean':None,'covered_mean':None,
                'coverage':0,'reason':'unaligned_clock_domains'}
    return {'formula_version':'resource.left_constant.v1','mean':integral/(end-start) if complete else None,
            'covered_mean':integral/covered_s if covered_s else None,'integral':integral,
            'covered_s':covered_s,'coverage':covered_s/(end-start)}


def cohort_pss(cohort,per_instance,*,shared_bytes=None,baseline_bytes=None):
    """Only a complete matching cohort permits full-cohort direct/amortized means."""
    identities=set(cohort)
    if len(identities)!=len(cohort):
        raise ValueError('duplicate cohort member')
    if set(per_instance)-identities:
        raise ValueError('resources outside cohort must be reported separately')
    observed={key:number(value,nonnegative=True) for key,value in per_instance.items() if value is not None}
    n=len(identities); seen=len(observed)
    total=sum(observed.values())
    complete=n>0 and seen==n
    for value in (shared_bytes,baseline_bytes):
        if value is not None:
            number(value,nonnegative=True)
    return {'formula_version':'cohort.pss.v1','cohort':sorted(identities),'observed_cohort':sorted(observed),
            'n':n,'observed_n':seen,'coverage':seen/n if n else 0,
            'observed_total_bytes':total,'observed_mean_bytes':total/seen if seen else None,
            'direct_mean_bytes':total/n if complete else None,
            'shared_bytes':shared_bytes,
            'with_shared_mean_bytes':(total+shared_bytes)/n if complete and shared_bytes is not None else None,
            'incremental_mean_bytes':(total+shared_bytes-baseline_bytes)/n if complete and shared_bytes is not None and baseline_bytes is not None else None}
