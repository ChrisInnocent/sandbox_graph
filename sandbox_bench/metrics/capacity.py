"""Byte-exact projection and separately captured, pinned awk compatibility view."""
from fractions import Fraction
import hashlib
import math
import os
from pathlib import Path
import re
import shutil
import subprocess

MIB=1048576


def capture_awk_rounding(delta_mib, requested, *, executable='awk', expected_sha256=None):
    """Capture the real awk printf result; pass expected_sha256 to enforce a lock.

    Persist this evidence and pin its digest on subsequent comparable runs. This
    adapter has I/O; projection itself consumes the resulting record purely.
    """
    if type(delta_mib) is not int or type(requested) is not int or requested <= 0:
        raise ValueError('integer delta and positive requested required')
    path=Path(shutil.which(executable) or executable).resolve()
    digest=hashlib.sha256(path.read_bytes()).hexdigest()
    if expected_sha256 is not None and digest != expected_sha256:
        raise ValueError('awk reference identity changed')
    env={**os.environ,'LC_ALL':'C','LANG':'C'}
    version=subprocess.run([str(path),'--version'],env=env,capture_output=True,text=True,timeout=5)
    if version.returncode:
        version=subprocess.run([str(path),'-W','version'],env=env,capture_output=True,text=True,timeout=5)
    version_text=(version.stdout+version.stderr).strip().splitlines()
    if version.returncode or not version_text:
        raise ValueError('cannot identify awk version')
    output=subprocess.run([str(path),'-v',f'd={delta_mib}','-v',f'n={requested}',
                           'BEGIN { printf "%.1f", d/n }'],env=env,capture_output=True,text=True,timeout=5,check=True).stdout
    if not re.fullmatch(r'-?\d+\.\d',output):
        raise ValueError('invalid awk decimal output')
    return {'delta_mib':delta_mib,'requested':requested,'rounded_mib':output,
            'reference_sha256':digest,'reference_version':version_text[0],'locale':'C',
            'formula':'awk printf %.1f (delta_mib/requested)'}


def projection(baseline_bytes,samples,requested,ready,reserve_bytes,*,domain,sample_domain,window,
               compatibility=None,window_kind='steady',window_version='bounded_observation.v1'):
    valid=(isinstance(window,(list,tuple)) and len(window)==2
           and all(type(value) in (int,float) and math.isfinite(value) for value in window)
           and window[1]>window[0] and isinstance(window_version,str) and bool(window_version))
    if not valid:
        return {'formula_version':'capacity.projection.v2','model_version':'linear_memavailable.v1',
                'window_version':window_version,'window':None,'window_kind':window_kind,'measured_capacity':False,
                'compatibility':{'status':'unavailable','reason':'missing_or_invalid_window_evidence'},
                'unified':{'status':'unavailable','reason':'missing_or_invalid_window_evidence'}}
    result=_projection_values(baseline_bytes,samples,requested,ready,reserve_bytes,domain=domain,
                              sample_domain=sample_domain,window=window,compatibility=compatibility,window_kind=window_kind)
    result.update(model_version='linear_memavailable.v1',window_version=window_version)
    return result


def _projection_values(baseline_bytes,samples,requested,ready,reserve_bytes,*,domain,sample_domain,window,compatibility=None,window_kind='steady'):
    """Return labelled non-measured estimates. samples are MemAvailable bytes."""
    result={'formula_version':'capacity.projection.v2','measured_capacity':False,
            'domain':domain,'window':window,'window_kind':window_kind,
            'compatibility':{'status':'unavailable','reason':'missing_locked_awk_reference'},
            'unified':{'status':'invalid'}}
    if not domain or domain != sample_domain:
        result['unified']={'status':'unavailable','reason':'memory_domain_mismatch'}
        return result
    if (any(type(x) is not int or x < 0 for x in [baseline_bytes,requested,ready,reserve_bytes])
            or requested==0 or not samples or any(type(x) is not int or x<0 for x in samples)):
        result['unified']['reason']='invalid_inputs'
        return result
    minimum=min(samples)
    a0_mib, amin_mib=baseline_bytes//MIB, minimum//MIB
    result['inputs']={'baseline_bytes':baseline_bytes,'minimum_bytes':minimum,'requested':requested,
                      'ready':ready,'reserve_bytes':reserve_bytes}
    if compatibility is not None:
        ref=compatibility
        if (ref.get('delta_mib') != a0_mib-amin_mib or ref.get('requested') != requested
                or ref.get('locale')!='C' or not ref.get('reference_sha256') or not ref.get('reference_version')):
            result['compatibility']={'status':'invalid','reason':'reference_mismatch'}
        else:
            try:
                cost=Fraction(ref['rounded_mib'])
            except (ValueError,KeyError,ZeroDivisionError):
                cost=None
            result['compatibility']={'status':'invalid','reference':ref,'denominator':'requested',
                                     'baseline_mib':a0_mib,'minimum_mib':amin_mib}
            if cost is not None and cost > Fraction(1,2):
                result['compatibility'].update(status='valid',per_requested_mib=float(cost),estimated_remaining=math.floor(Fraction(amin_mib)/cost))
            else:
                result['compatibility']['reason']='rounded_cost_not_above_half_mib'
    if ready != requested:
        result['unified']['reason']='not_all_requested_healthy'
    elif baseline_bytes <= reserve_bytes:
        result['unified']['reason']='invalid_reserve'
    elif baseline_bytes <= minimum:
        result['unified']['reason']='nonpositive_cost'
    else:
        cost=Fraction(baseline_bytes-minimum,ready)
        total=math.floor(Fraction(baseline_bytes-reserve_bytes)/cost)
        result['unified'].update(per_ready_bytes=float(cost),cost_numerator=cost.numerator,cost_denominator=cost.denominator)
        if total < ready:
            result['unified'].update(reason='model_budget_conflict',candidate_total=total)
        else:
            result['unified'].update(status='valid',estimated_total=total,estimated_additional=total-ready)
    return result
