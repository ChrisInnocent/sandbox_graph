"""Creation followed by an independently covered, bounded retained cohort window."""
import asyncio
import time

HOLD_METHODS=('B01.TEMPLATE.HOLD','B01.SNAPSHOT.DISK.HOLD','B01.SNAPSHOT.MEMORY.HOLD',
    'B02.FULL.HOLD','B02.PREPARED.HOLD')
HOLD_GRAPH_REF='hold:1'


def base_method(method):
    return method.removesuffix('.HOLD') if method in HOLD_METHODS else method


def covered_window(rows,*,started,contract,ids,clock_domain):
    rows=sorted((r for r in rows if r.get('kind')=='resource_sample' and r.get('t_s',-1)>=started),
        key=lambda row:row['t_s'])
    if not rows: return None
    start=rows[0]['t_s'];end=start+contract.window_s
    last=next((i for i,row in enumerate(rows) if row['t_s']>=end),None)
    if last is None: return None
    selected=rows[:last+1]
    if len(selected)<contract.min_samples: return None
    if len({r.get('sample_id') for r in selected})!=len(selected): return None
    if any(b['t_s']-a['t_s']>contract.max_gap_s for a,b in zip(selected,selected[1:])): return None
    for row in selected:
        if (row.get('clock_domain')!=clock_domain or row.get('inventory_complete') is not True
                or set(row.get('snapshot_cohort_ids',()))!=set(ids) or not row.get('processes')
                or any(p.get('pss_bytes') is None or p.get('cpu_seconds') is None for p in row['processes'])):
            return None
    return {'start_s':start,'end_s':end,'clock_domain':clock_domain,'window_kind':'creation_hold'},[r['sample_id'] for r in selected]


async def hold_created(case,ids,*,inspect,steady,samples,clock_domain,clock=time.monotonic,sleep=asyncio.sleep):
    if case.method_id not in HOLD_METHODS or case.resources is None:
        raise ValueError('registered creation HOLD contract required')
    started=clock();deadline=started+case.resources.max_wait_s
    ids=tuple(sorted(ids))
    result={'schema_version':'creation_hold.v1','case_hash':case.digest(),
        'requested_count':case.load.requests,'owned_ids':list(ids),'actual_ready_count':0,
        'ready_ids':[],'resource_status':'unavailable','status':'blocked','started_s':started}
    if not ids or len(ids)!=len(set(ids)):
        return result|{'reason':'empty_or_duplicate_owned_cohort'}
    async def ready():
        found=[]
        for sid in ids:
            remaining=deadline-clock()
            if remaining<=0: raise TimeoutError('hold deadline')
            row=await asyncio.wait_for(inspect({'sandbox_id':sid},min(remaining,case.completion.timeout_s)),remaining)
            if row.get('state')==case.completion.target_state: found.append(sid)
        return found
    try:
        initial=await ready();result.update(ready_ids=initial,actual_ready_count=len(initial))
        if len(initial)!=len(ids): return result|{'reason':'owned_cohort_not_ready'}
        observed=await asyncio.wait_for(steady(),max(0,deadline-clock()))
        if observed.get('status')!='completed': return result|{'reason':'steady_window_unavailable','steady':observed}
        if clock()<started+case.resources.window_s:
            await sleep(started+case.resources.window_s-clock())
        # First retained window only; never skip an inconvenient earlier sample.
        coverage=None
        while clock()<=deadline:
            coverage=covered_window(samples(),started=started,contract=case.resources,ids=ids,clock_domain=clock_domain)
            if coverage or clock()+case.resources.interval_s>=deadline: break
            await sleep(case.resources.interval_s)
        final=await ready();result.update(ready_ids=final,actual_ready_count=len(final))
        if final!=initial: return result|{'reason':'cohort_changed_during_hold'}
        if coverage is None: return result|{'reason':'hold_resource_coverage_insufficient'}
        window,refs=coverage
        return result|{'status':'completed','resource_status':'observed','window':window,
            'sample_refs':refs,'finished_s':clock(),'steady':observed.get('steady')}
    except (OSError,ValueError,TimeoutError) as exc:
        return result|{'reason':'hold_observation_'+type(exc).__name__,'finished_s':clock()}
