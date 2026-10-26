"""Bounded sched_switch observation. Trace windows never borrow sampler clocks."""
from __future__ import annotations

import asyncio
import copy
import threading
import time
from pathlib import Path

from .sched_peak import attribution_from_inventory, peak_from_sched


def local_threads(rows, proc_root='/proc'):
    """Verify start ticks and enumerate TIDs; unknown/reused tasks fail closed."""
    root=Path(proc_root)
    boot=(root/'sys/kernel/random/boot_id').read_text().strip()
    result=[]
    for row in rows:
        identity=row['identity'];pid=identity['pid']
        stat=(root/str(pid)/'stat').read_text().rsplit(')',1)[1].split()
        if identity['boot_id']!=boot or int(stat[19])!=identity['start_ticks']:
            raise ValueError('sched process identity changed')
        for task in sorted((root/str(pid)/'task').iterdir()):
            raw=(task/'stat').read_text().rsplit(')',1)[1].split()
            result.append({**row,'identity':{**identity,'pid':int(task.name),'start_ticks':int(raw[19])},
                'leader_identity':identity})
    if not result: raise ValueError('sched task inventory empty')
    attribution_from_inventory(result)
    return result


def analyze_trace(record, window='create'):
    unavailable={'status':'unavailable','true_peak_cores':None,'sampled_peak_cores':None,
        'by_role':{},'by_component':{}}
    if record.get('status')!='completed':
        return unavailable|{'reason':record.get('reason') or 'trace_stop_unconfirmed'}
    span=record.get('windows',{}).get(window)
    if not span: return unavailable|{'reason':'trace_window_missing'}
    if span.get('clock_domain')!=record.get('clock_domain'):
        return unavailable|{'reason':'trace_window_clock_mismatch'}
    if record.get('inventory_error'):
        return unavailable|{'reason':'trace_inventory_unverified'}
    try:
        result=peak_from_sched(record['events'],attribution=attribution_from_inventory(record['inventory']),
            window_start_ns=span['start_ns'],window_end_ns=span['end_ns'],clock_domain=span['clock_domain'])
    except (ValueError,KeyError,TypeError):
        return unavailable|{'reason':'invalid_trace_evidence'}
    result.update(sampled_peak_cores=None,by_role=result.pop('true_peak_cores_by_role'),
        by_component=result.pop('true_peak_cores_by_component'))
    if record.get('lost_events') or record.get('dropped_events'):
        result.update(status='partial',reason='trace_events_lost')
    elif record.get('inventory_changed'):
        result.update(status='partial',reason='inventory_changed')
    if result['status']=='partial':
        # Observed concurrency is a lower bound, not an exact peak.
        result['observed_peak_lower_bound_cores']=result['true_peak_cores']
        result['true_peak_cores']=None
    return result


class SchedObservation:
    def __init__(self,collector,*,inventory,clock_ns=time.monotonic_ns,
                 pre_roll_s=.05,post_roll_s=.05,verify_inventory=local_threads):
        if not 0<=pre_roll_s<=10 or not 0<=post_roll_s<=10:
            raise ValueError('bounded sched pre/post roll required')
        self.collector=collector;self.inventory=inventory;self.verify_inventory=verify_inventory
        self.clock_ns=clock_ns;self.pre_roll_s=pre_roll_s;self.post_roll_s=post_roll_s
        self._stop=threading.Event();self._thread=None;self._result=None;self._poll_error=None
        self.record={'schema_version':'sched_observation.v1','status':'not_started',
            'clock_domain':collector.clock_domain,'windows':{}}

    def _poll(self):
        try:
            while not self._stop.is_set():
                self.collector.poll(timeout_ms=10)
                self._stop.wait(.001)
        except Exception as exc: self._poll_error=type(exc).__name__

    async def start(self):
        if self.record['status']!='not_started': return copy.deepcopy(self.record)
        try:
            self.record['inventory']=copy.deepcopy(self.verify_inventory(self.inventory()))
            self.collector.start()
        except Exception as exc:
            self.record.update(status='unavailable',reason='sched_backend_'+type(exc).__name__)
            return copy.deepcopy(self.record)
        self.record.update(status='running',started_ns=self.clock_ns())
        self._thread=threading.Thread(target=self._poll,name='sched-observer',daemon=True)
        self._thread.start()
        await asyncio.sleep(self.pre_roll_s)
        return copy.deepcopy(self.record)

    def begin(self,name):
        if self.record['status']!='running': return
        if name in self.record['windows']: raise ValueError('trace window already opened')
        self.record['windows'][name]={'start_ns':self.clock_ns(),'clock_domain':self.collector.clock_domain}

    def end(self,name):
        if self.record['status']!='running': return
        window=self.record['windows'][name]
        if 'end_ns' in window: raise ValueError('trace window already closed')
        window['end_ns']=self.clock_ns()

    async def stop(self,*,timeout_s):
        if self._result is not None: return copy.deepcopy(self._result)
        if self.record['status']!='running':
            return copy.deepcopy(self.record)
        if not self._stop.is_set():
            await asyncio.sleep(min(self.post_roll_s,timeout_s))
            self._stop.set()
        await asyncio.to_thread(self._thread.join,max(0,timeout_s-self.post_roll_s))
        if self._thread.is_alive(): return self.record|{'status':'unknown','reason':'sched_poll_exit_unconfirmed'}
        try:
            final=self.verify_inventory(self.inventory())
            self.record['inventory_changed']=final!=self.record['inventory']
            # Keep initial identities: changes are explicit, never reassigned to new PIDs.
        except (OSError,ValueError,KeyError): self.record['inventory_error']='end_inventory_unverified'
        try:
            events=self.collector.stop()
        except Exception:
            return self.record|{'status':'unknown','reason':'sched_detach_unconfirmed'}
        self.record.update(status='completed',events=events,finished_ns=self.clock_ns(),
            lost_events=self.collector.lost_events,dropped_events=self.collector.dropped_events)
        if self._poll_error:self.record.update(status='unavailable',reason='sched_poll_'+self._poll_error)
        self._result=copy.deepcopy(self.record)
        return copy.deepcopy(self._result)
