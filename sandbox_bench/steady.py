"""Select the first eligible evidence window, never the best later segment."""
from dataclasses import dataclass
from .metrics.common import number


@dataclass(frozen=True)
class SteadyConfig:
    window_s: float
    min_samples: int
    max_gap_s: float
    cohort_stability: bool
    metric_bounds: tuple
    slope_bounds: tuple
    max_wait_s: float

    def __post_init__(self):
        for name in ('window_s','max_gap_s','max_wait_s'):
            if number(getattr(self,name),name,nonnegative=True)<=0:
                raise ValueError(f'{name} must be positive')
        if type(self.min_samples) is not int or self.min_samples<2:
            raise ValueError('min_samples must be at least 2')
        for limits in (self.metric_bounds,self.slope_bounds):
            if len(limits)!=2 or number(limits[0])>number(limits[1]):
                raise ValueError('invalid bounds')


def find_steady_window(samples,config,*,started_s=None):
    rows=list(samples)
    base=rows[0]['t_s'] if rows and started_s is None else (started_s or 0)
    limit=base+config.max_wait_s
    if any(number(b['t_s'])<=number(a['t_s']) for a,b in zip(rows,rows[1:])):
        raise ValueError('steady samples must be chronological and unique')
    evidence_windows=0
    for right,row in enumerate(rows):
        end=number(row['t_s'])
        if end>limit:
            break
        start=end-config.window_s
        if start<base:
            continue
        # Require a sample at/before the left boundary, retaining the crossing pair.
        candidates=[i for i in range(right+1) if rows[i]['t_s']<=start]
        if not candidates:
            continue
        left=candidates[-1]
        selected=rows[left:right+1]
        in_window=[r for r in selected if r['t_s']>=start]
        if len(in_window)<config.min_samples:
            continue
        if any(r.get('value') is None or not r.get('clock_domain') for r in selected):
            continue
        if len({r['clock_domain'] for r in selected})!=1:
            continue
        if any(b['t_s']-a['t_s']>config.max_gap_s for a,b in zip(selected,selected[1:])):
            continue
        if config.cohort_stability and (
                any('cohort' not in r or r['cohort'] is None for r in selected)
                or any(r.get('cohort')!=selected[0].get('cohort') for r in selected[1:])):
            continue
        evidence_windows+=1
        values=[number(r['value']) for r in selected]
        slope=(values[-1]-values[0])/(selected[-1]['t_s']-selected[0]['t_s'])
        low,high=config.metric_bounds
        slo,shi=config.slope_bounds
        if all(low<=v<=high for v in values) and slo<=slope<=shi:
            return {'formula_version':'steady.first_window.v1','status':'steady','start_s':start,
                    'end_s':end,'cohort':selected[0].get('cohort'),'clock_domain':selected[0]['clock_domain'],
                    'sample_count':len(in_window),'slope':slope}
    return {'formula_version':'steady.first_window.v1',
            'status':'not_steady' if evidence_windows else 'insufficient_evidence',
            'max_wait_s':config.max_wait_s,'eligible_windows':evidence_windows}
