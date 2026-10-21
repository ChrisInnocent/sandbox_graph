"""Density protocols: observed growth and explicitly labelled linear estimates."""

import subprocess


MIB = 1024 * 1024


def _integer(value, minimum=0):
    return type(value) is int and value >= minimum


def projection(baseline_bytes: int, samples: list[dict], requested: int,
               ready: int, reserve_bytes: int, *, awk_reference_sha256=None) -> dict:
    """Legacy API wrapper over the shared byte/awk formulas.

    Existing callers did not carry host/window identity. Their result explicitly
    remains legacy_domain_unverified; new sessions should use metrics.projection
    with actual domain and window evidence. awk absence only blocks legacy view.
    """
    from .metrics.capacity import _projection_values as calculate, capture_awk_rounding
    inputs = {"baseline_bytes": baseline_bytes, "requested": requested,
              "ready": ready, "reserve_bytes": reserve_bytes}
    result = {"method_version":"resource_projection.v2",
              "provenance_status":"legacy_domain_unverified",
              "inputs":{key:value if type(value) is int else None for key,value in inputs.items()},
              "legacy":{"status":"invalid"},"unified":{"status":"invalid"}}
    result['inputs']['sample_count']=len(samples) if isinstance(samples,list) else None
    def invalid(reason):
        result.update(status='invalid',reason=reason)
        result['unified']['reason']=reason
        result['legacy'].setdefault('reason',reason)
        return result
    if not _integer(baseline_bytes,1) or not _integer(requested,1) or not _integer(ready):
        return invalid('invalid baseline/requested/ready')
    if not isinstance(samples,list) or len(samples)<2:
        return invalid('at least two memory samples are required')
    values=[]
    for row in samples:
        value=row.get('mem_available_bytes') if isinstance(row,dict) else None
        if not _integer(value):
            return invalid('nonnegative integer MemAvailable bytes required')
        values.append(value)
    if not _integer(reserve_bytes):
        return invalid('nonnegative integer reserve required')
    minimum=min(values)
    result['inputs'].update(min_available_bytes=minimum,delta_bytes=baseline_bytes-minimum)
    compatibility=None
    capture_reason=None
    try:
        compatibility=capture_awk_rounding(baseline_bytes//MIB-minimum//MIB,requested,
                                           expected_sha256=awk_reference_sha256)
    except (OSError,ValueError,subprocess.SubprocessError) as exc:
        capture_reason=type(exc).__name__
    computed=calculate(baseline_bytes,values,requested,ready,reserve_bytes,
                       domain='legacy-unverified',sample_domain='legacy-unverified',
                       window=None,window_kind='legacy-unspecified',compatibility=compatibility)
    result['legacy']=dict(computed['compatibility'])
    if capture_reason:
        result['legacy'].update(status='unavailable',reason='awk_reference_unavailable',error_type=capture_reason)
    if 'estimated_remaining' in result['legacy']:
        result['legacy']['legacy_estimate_remaining']=result['legacy'].pop('estimated_remaining')
    if 'minimum_mib' in result['legacy']:
        result['legacy']['min_available_mib']=result['legacy'].pop('minimum_mib')
    result['unified']=computed['unified']
    result['status']=result['unified']['status']
    result['reason']=result['unified'].get('reason')
    return result


class GrowthTracker:
    """Track count deltas; a stop reason never asserts a physical capacity limit."""

    def __init__(self, batch_size=100, min_growth=10, stall_limit=2,
                 max_instances=2000, max_batches=200):
        parameters = dict(batch_size=batch_size, min_growth=min_growth,
                          stall_limit=stall_limit, max_instances=max_instances,
                          max_batches=max_batches)
        if not all(_integer(value, 1) for value in parameters.values()):
            raise ValueError("growth parameters must be positive integers")
        if batch_size < min_growth:
            raise ValueError("batch_size must be at least min_growth")
        self.parameters = parameters
        self.batch = self.listed = self.ready = self.stalls = 0
        self.ever_ready = False
        self.unready_batches = 0
        self.stop_reason = None

    def update(self, listed: int, ready: int) -> dict:
        if not _integer(listed) or not _integer(ready) or ready > listed:
            raise ValueError("counts must be nonnegative integers with ready <= listed")
        if self.stop_reason is not None:
            raise ValueError("growth tracking has already stopped")
        net_growth, ready_growth = listed - self.listed, ready - self.ready
        self.batch += 1
        self.stalls = self.stalls + 1 if net_growth < self.parameters["min_growth"] else 0
        self.ever_ready = self.ever_ready or ready > 0
        self.unready_batches = self.unready_batches + 1 if ready == 0 else 0
        if not self.ever_ready and self.unready_batches >= self.parameters["stall_limit"]:
            self.stop_reason = "setup_failure"
        elif listed >= self.parameters["max_instances"]:
            self.stop_reason = "instance_cap"
        elif self.batch >= self.parameters["max_batches"]:
            self.stop_reason = "batch_cap"
        elif self.stalls >= self.parameters["stall_limit"]:
            self.stop_reason = "plateau"
        self.listed, self.ready = listed, ready
        return {
            "batch": self.batch, "listed": listed, "ready": ready,
            "net_growth": net_growth, "ready_growth": ready_growth,
            # Aggregate counts can prove a net loss, but cannot identify churn.
            "lost_ready": max(0, -ready_growth),
            "stall_batches": self.stalls, "stop_reason": self.stop_reason,
            "functional": ready > 0, "parameters": dict(self.parameters),
        }
