"""Exact CPU peak cores from scheduler tracepoints, attributed per process class.

The kernel exposes no CPU-peak high-water counter (unlike ``memory.peak``); the only
exact method is to reconstruct each task's on-CPU intervals from ``sched_switch`` and
take the maximum concurrency. Collection is per process class: a Kuasar-style run
attributes the sandbox (work) process and the sandbox-management process separately,
reusing the sampler's ``roles``/``components`` attribution model.

``true_peak_cores`` is the union over scoped tasks (each counted once); ``by_role`` and
``by_component`` are labeled *views* that may overlap and must never be summed.

No sampling error, but exactness requires that every scoped task's on/off is captured
inside a pre-roll/post-roll around the measured window. Tasks already on-CPU at the
start (unbounded) or still on at the end are clipped and reported as ``partial`` — the
value never pretends to full coverage.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

SCHED_PEAK_FORMULA = 'sched_peak.v1'

# bcc program: record (timestamp, prev_pid, next_pid) for every context switch.
# sched_switch args: prev_comm, prev_pid, prev_prio, prev_state, next_comm, next_pid, next_prio
_BCC_PROGRAM = r'''
from bcc import BPF

BPF_SOURCE = r"""
struct event_t { u64 t_ns; s32 prev_pid; s32 next_pid; };
BPF_PERF_OUTPUT(events);
TRACEPOINT_PROBE(sched, sched_switch) {
    struct event_t e = {};
    e.t_ns = bpf_ktime_get_ns();
    e.prev_pid = args->prev_pid;
    e.next_pid = args->next_pid;
    events.perf_submit(args, &e, sizeof(e));
    return 0;
}
"""
'''


def attribution_from_inventory(rows):
    """Build a pid -> class map from sampler-style inventory rows.

    Rows carry ``identity.pid`` plus ``roles``/``components`` and optional
    ``sandbox_id``/``generation``. Thread rows attach to their verified leader. Each pid
    must map to exactly one full identity; a reused pid is rejected as unattributable.
    """
    attribution = {}
    by_pid = {}
    for row in rows:
        identity = row.get('thread_group_identity') or row.get('identity')
        if not isinstance(identity, dict) or not isinstance(identity.get('pid'), int):
            raise ValueError('inventory row requires an integer process identity pid')
        pid = identity['pid']
        if pid <= 0:
            raise ValueError('positive pid required')
        if pid in by_pid and by_pid[pid] != identity:
            raise ValueError('pid maps to two identities; pid reuse is not attributable')
        by_pid[pid] = identity
        entry = {'identity': identity, 'roles': list(row.get('roles') or []),
                 'components': list(row.get('components') or []),
                 'sandbox_id': row.get('sandbox_id'), 'generation': row.get('generation')}
        existing = attribution.get(pid)
        if existing is not None and existing != entry:
            raise ValueError('conflicting attribution for one pid')
        attribution[pid] = entry
    if not attribution:
        raise ValueError('non-empty inventory required')
    return attribution


def reconstruct_on_cpu(events, *, scope_pids, window_start_ns, window_end_ns):
    """Rebuild per-task on-CPU intervals from sched_switch events.

    Each event is ``{'t_ns': int, 'prev_pid': int, 'next_pid': int}`` where ``prev_pid``
    leaves the CPU and ``next_pid`` enters it at ``t_ns``. Only scoped pids are tracked;
    idle (-1) and out-of-scope tasks are ignored.
    """
    scope = set(scope_pids)
    if not scope:
        raise ValueError('non-empty scope required')
    if not all(isinstance(pid, int) and pid > 0 for pid in scope):
        raise ValueError('positive integer scope pids required')
    if not isinstance(window_start_ns, int) or not isinstance(window_end_ns, int) or window_end_ns <= window_start_ns:
        raise ValueError('positive integer window required')
    on_since = {}
    intervals = []
    gaps = []
    unbounded = set()
    for event in sorted(events, key=lambda row: row['t_ns']):
        t = event['t_ns']
        prev_pid, next_pid = event['prev_pid'], event['next_pid']
        if not isinstance(t, int) or not isinstance(prev_pid, int) or not isinstance(next_pid, int):
            raise ValueError('sched event requires integer t_ns/prev_pid/next_pid')
        if prev_pid in scope:
            if prev_pid in on_since:
                intervals.append({'pid': prev_pid, 'start_ns': on_since.pop(prev_pid), 'end_ns': t})
            else:
                # Already on-CPU before observation began: clip to the window and flag.
                unbounded.add(prev_pid)
                intervals.append({'pid': prev_pid, 'start_ns': min(window_start_ns, t),
                                  'end_ns': t, 'clipped_start': True})
        if next_pid in scope:
            if next_pid in on_since:
                gaps.append({'pid': next_pid, 't_ns': t, 'reason': 'double_on'})
            else:
                on_since[next_pid] = t
    for pid, start in on_since.items():
        end = window_end_ns
        clipped = start < window_end_ns
        intervals.append({'pid': pid, 'start_ns': start, 'end_ns': end, 'clipped_end': clipped})
        unbounded.add(pid)
    return {'formula_version': 'sched_on_cpu.v1', 'intervals': intervals,
            'gaps': gaps, 'unbounded_pids': sorted(unbounded)}


def _sweep_peak(points):
    """Max concurrency via sweep line; at equal timestamps ends (-1) precede starts (+1)."""
    current = peak = 0
    for _, delta in sorted(points, key=lambda item: (item[0], item[1])):
        current += delta
        peak = max(peak, current)
    return peak


def peak_from_sched(events, *, attribution, window_start_ns, window_end_ns, clock_domain):
    """Exact peak CPU cores (union) plus per-role/per-component views.

    ``attribution`` is ``{pid: {'roles': [...], 'components': [...], ...}}``; every scoped
    pid is tracked and counted once in the union. Views are not additive.
    """
    if not isinstance(attribution, dict) or not attribution:
        raise ValueError('non-empty attribution required')
    if not isinstance(clock_domain, str) or not clock_domain:
        raise ValueError('explicit trace clock domain required')
    recon = reconstruct_on_cpu(events, scope_pids=set(attribution),
                               window_start_ns=window_start_ns, window_end_ns=window_end_ns)
    clipped = []
    for interval in recon['intervals']:
        a = max(window_start_ns, interval['start_ns'])
        b = min(window_end_ns, interval['end_ns'])
        if b > a:
            clipped.append((interval['pid'], a, b))
    total_points = []
    role_points = {}
    component_points = {}
    for pid, a, b in clipped:
        total_points += [(a, 1), (b, -1)]
        labels = attribution[pid]
        for label in set(labels.get('roles') or []):
            role_points.setdefault(label, []).extend([(a, 1), (b, -1)])
        for label in set(labels.get('components') or []):
            component_points.setdefault(label, []).extend([(a, 1), (b, -1)])
    status = 'valid' if not recon['unbounded_pids'] and not recon['gaps'] else 'partial'
    return {'formula_version': SCHED_PEAK_FORMULA, 'status': status,
            'true_peak_cores': float(_sweep_peak(total_points)) if clipped else None,
            'true_peak_cores_by_role': {key: float(_sweep_peak(value)) for key, value in sorted(role_points.items())},
            'true_peak_cores_by_component': {key: float(_sweep_peak(value)) for key, value in sorted(component_points.items())},
            'window': {'start_ns': window_start_ns, 'end_ns': window_end_ns, 'clock_domain': clock_domain},
            'interval_count': len(clipped),
            'unbounded_pids': recon['unbounded_pids'], 'gaps': recon['gaps']}


class BpfSchedSwitchCollector:
    """Optional exact CPU-peak collector over the sched_switch tracepoint.

    Availability requires a BPF runtime (bcc) and privileges; otherwise the collector is
    unavailable and callers must fall back to ``sampled_peak_cores``. This class never
    fabricates a peak; a missing observation is reported as unavailable, not zero.
    """

    def __init__(self, *, run_id, clock_domain, ring_capacity=2_000_000):
        if not run_id or not clock_domain:
            raise ValueError('run ID and trace clock domain required')
        if not isinstance(ring_capacity, int) or ring_capacity <= 0:
            raise ValueError('positive ring capacity required')
        self.run_id = run_id
        self.clock_domain = clock_domain
        self.ring_capacity = ring_capacity
        self._events = []
        self._bpf = None
        self.lost_events = 0
        self.dropped_events = 0

    @staticmethod
    def availability():
        if os.geteuid() != 0:
            return {'available': False, 'reason': 'requires_root_or_cap_bpf'}
        if shutil.which('clang') is None:
            return {'available': False, 'reason': 'clang_missing'}
        import importlib.util
        if importlib.util.find_spec('bcc') is None:
            return {'available': False, 'reason': 'bcc_not_installed'}
        for base in ('/sys/kernel/tracing', '/sys/kernel/debug/tracing'):
            if (Path(base) / 'events/sched/sched_switch').exists():
                return {'available': True, 'reason': None, 'backend': 'bcc', 'tracefs': base}
        return {'available': False, 'reason': 'tracefs_unavailable'}

    def start(self):  # pragma: no cover - requires a live privileged BPF runtime
        status = self.availability()
        if not status['available']:
            raise RuntimeError(f'bpf sched collector unavailable: {status["reason"]}')
        from bcc import BPF  # local import: optional dependency
        self._bpf = BPF(text=_BCC_PROGRAM.split('BPF_SOURCE = r"""', 1)[1].rsplit('"""', 1)[0])
        self._bpf['events'].open_perf_buffer(self._on_event, page_cnt=256, lost_cb=self._on_lost)
        return status

    def _on_event(self, cpu, data, size):  # pragma: no cover - live only
        event = self._bpf['events'].event(data)
        if len(self._events) < self.ring_capacity:
            self._events.append({'t_ns': int(event.t_ns), 'prev_pid': int(event.prev_pid),
                                 'next_pid': int(event.next_pid)})
        else:
            self.dropped_events += 1

    def _on_lost(self, cpu, count):
        self.lost_events += count

    def poll(self, timeout_ms=1000):  # pragma: no cover - live only
        if self._bpf is not None:
            self._bpf.perf_buffer_poll(timeout=timeout_ms)

    def stop(self):  # pragma: no cover - live only
        if self._bpf is not None:
            self._bpf.cleanup()
            self._bpf = None
        events, self._events = self._events, []
        return events

    def peak(self, *, attribution, window_start_ns, window_end_ns):  # pragma: no cover - live only
        events = self.stop()
        return peak_from_sched(events, attribution=attribution, window_start_ns=window_start_ns,
                               window_end_ns=window_end_ns, clock_domain=self.clock_domain)
