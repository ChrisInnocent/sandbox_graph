"""Finite B09/B10 workflows with independent cohort and resource observations.

Providers supply measured facts and provenance; the controller never installs,
creates a synthetic resource sample, substitutes requested count for live IDs, or
labels a budget/plateau stop as physical capacity. Cohort snapshots are scoped to
this test population, node, environment and inventory generation.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import math
import time
from typing import Awaitable, Callable, Any

from .contracts import CaseContract
from .load import LoadEngine, LoadReport, LoadSchedule, OperationResult, SubmissionJournal
from .metrics.capacity import projection


@dataclass(frozen=True)
class CohortSnapshot:
    ids: tuple[str, ...]
    ready_ids: tuple[str, ...]
    node: str
    environment_ref: str
    scope_ref: str
    complete: bool
    paused_ids: tuple[str, ...] = ()

    def __post_init__(self):
        for group in (self.ids, self.ready_ids, self.paused_ids):
            if len(set(group)) != len(group) or any(not isinstance(sid,str) or not sid for sid in group):
                raise ValueError('cohort identifiers must be unique and nonempty')
        if not set(self.ready_ids) <= set(self.ids) or not set(self.paused_ids) <= set(self.ids):
            raise ValueError('state subsets must belong to the observed cohort')
        if set(self.ready_ids) & set(self.paused_ids):
            raise ValueError('one sandbox cannot simultaneously be ready and paused')
        if not self.node or not self.environment_ref or not self.scope_ref or type(self.complete) is not bool:
            raise ValueError('cohort provenance and completeness are required')


@dataclass
class DensityReport:
    method_id: str
    status: str = 'complete'
    stop_reason: str = 'budget_exhausted'
    normalized_stop_reason: str | None = None
    baseline: dict | None = None
    final_cohort: CohortSnapshot | None = None
    trace: list[dict] = field(default_factory=list)
    load_reports: list[LoadReport] = field(default_factory=list)
    resource_observations: list[dict] = field(default_factory=list)
    created_count: int = 0
    projection: dict | None = None
    slo: dict | None = None
    capacity_measured: bool = False
    creation_throughput_status: str = 'applicable'


class _OffsetJournal:
    def __init__(self, journal: SubmissionJournal, offset: int):
        self.journal, self.offset = journal, offset
    async def reserve(self,index,weight):return await self.journal.reserve(index+self.offset,weight)
    async def submission_started(self,token):return await self.journal.submission_started(token)
    async def record_terminal(self,token,result):return await self.journal.record_terminal(token,result)


class DensityController:
    def __init__(self, case: CaseContract, *,
                 cohort_inspector: Callable[[], Awaitable[CohortSnapshot]],
                 resource_snapshot: Callable[[], Awaitable[dict]],
                 pause_cohort: Callable[[tuple[str, ...]], Awaitable[None]] | None = None,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep):
        if not case.method_id.startswith(('B09.', 'B10.')):
            raise ValueError('DensityController requires a B09/B10 contract')
        self.case, self.cohort_inspector, self.resource_snapshot = case, cohort_inspector, resource_snapshot
        self.pause_cohort, self.clock, self.sleep = pause_cohort, clock, sleep

    async def _bounded(self, callback, deadline):
        if self.clock() >= deadline:raise TimeoutError
        task=asyncio.create_task(callback())
        timer=asyncio.create_task(self.sleep(deadline-self.clock()))
        try:
            done,_=await asyncio.wait((task,timer),return_when=asyncio.FIRST_COMPLETED)
            if task in done and self.clock()<deadline:return task.result()
            raise TimeoutError
        finally:
            task.cancel();timer.cancel()
            await asyncio.gather(task,timer,return_exceptions=True)

    async def run(self, operation: Callable[[int], Awaitable[OperationResult]], *,
                  journal: SubmissionJournal) -> DensityReport:
        case, method = self.case, self.case.method_id
        report=DensityReport(method)
        began=self.clock()
        deadline=began+case.load.run_timeout_s
        initial=None
        known_created: set[str]=set()
        allocated=0
        unknown=0

        def stop(status,reason):
            report.status,report.stop_reason=status,reason
            return report

        async def inspect_cohort():
            nonlocal initial
            snapshot=await self._bounded(self.cohort_inspector,deadline)
            if not isinstance(snapshot,CohortSnapshot):raise ValueError('invalid cohort snapshot')
            if not snapshot.complete:
                stop('insufficient','incomplete_inventory');return None
            if snapshot.environment_ref!=case.environment_ref:
                stop('invalid','environment_mismatch');return None
            if case.density is not None and snapshot.node!=case.density.target_node:
                stop('invalid','placement_mismatch');return None
            if initial is not None and (snapshot.node,snapshot.scope_ref)!=(initial.node,initial.scope_ref):
                stop('invalid','cohort_scope_changed');return None
            report.final_cohort=snapshot
            return snapshot

        async def resource():
            row=await self._bounded(self.resource_snapshot,deadline)
            if not isinstance(row,dict) or not row.get('evidence_ref') or not row.get('domain'):
                raise ValueError('resource observation requires provenance and domain')
            report.resource_observations.append(row)
            return row

        async def batch(count, *, mode='WINDOW'):
            nonlocal allocated,unknown
            offset=allocated
            remaining=deadline-self.clock()
            if remaining<=0:raise TimeoutError
            schedule=LoadSchedule(mode,count,case.load.concurrency,case.completion.timeout_s,
                batch_size=case.load.batch_size if mode=='BATCH' else None,
                gap=case.load.batch_gap_s if mode=='BATCH' else 0,
                scheduled_at=tuple(i/case.load.arrival_rate for i in range(count)) if mode=='OPEN_LOOP' else (),
                late_policy=case.load.late_policy if mode=='OPEN_LOOP' else 'drop',
                max_queue=case.load.max_queue if mode=='OPEN_LOOP' else 0,
                unknown_limit=max(0,case.load.max_unknown_external-unknown),deadline=remaining)
            async def call(local_index):return await operation(offset+local_index)
            load=await LoadEngine(clock=self.clock,sleep=self.sleep).run(schedule,call,
                                                                       journal=_OffsetJournal(journal,offset))
            report.load_reports.append(load)
            allocated+=load.allocated
            unknown+=load.unresolved_external
            report.created_count+=load.successes
            for result in load.results.values():
                if isinstance(result.value,dict) and isinstance(result.value.get('sandbox_id'),str):
                    known_created.add(result.value['sandbox_id'])
            return load

        try:
            if method=='B09.PAUSED' and self.pause_cohort is None:
                return stop('blocked','durably_authorized_pause_callback_required')
            initial=await inspect_cohort()
            if initial is None:return report
            if method.startswith('B09.') and initial.ids:
                return stop('invalid','platform_baseline_not_empty')
            if case.density and len(initial.ids)!=case.density.initial_count:
                return stop('invalid','initial_cohort_mismatch')
            if method=='B10.PROJECTION' and initial.ids:
                return stop('invalid','projection_requires_zero_instance_baseline')
            report.baseline=await resource()
            if method=='B09.PLATFORM':
                report.creation_throughput_status='not_applicable'
                report.stop_reason='observation_complete'
                return report
            if method=='B10.SLO_VERIFY':
                report.creation_throughput_status='not_applicable'
                if not initial.ids:return stop('blocked','slo_requires_existing_cohort')
                checks={}
                for name,threshold in case.density.slo.items():
                    metric=report.baseline.get('metrics',{}).get(name,{})
                    value=metric.get('value')
                    op=metric.get('operator')
                    if (isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value)
                            or op not in {'<=','>='} or not metric.get('evidence_ref')):
                        checks[name]={'status':'not_evaluated','reason':'missing_value_direction_or_evidence'}
                    else:
                        passed=value<=threshold if op=='<=' else value>=threshold
                        checks[name]={'status':'pass' if passed else 'fail','value':value,'threshold':threshold,
                                      'operator':op,'evidence_ref':metric['evidence_ref']}
                states={check['status'] for check in checks.values()}
                status='not_evaluated' if 'not_evaluated' in states else 'fail' if 'fail' in states else 'pass'
                report.slo={'status':status,'checks':checks}
                report.stop_reason='verification_complete'
                return report
            if method.startswith('B10.GROWTH'):
                density=case.density
                previous=initial
                stalls=0
                ever_ready=bool(initial.ready_ids)
                ever_listed=bool(initial.ids)
                for batch_index in range(density.max_batches):
                    count=min(density.batch_size,case.load.requests-allocated,density.max_instances-len(previous.ids))
                    if count<=0:
                        report.stop_reason='instance_cap' if len(previous.ids)>=density.max_instances else 'request_budget'
                        break
                    load=await batch(count)
                    current=await inspect_cohort()
                    if current is None:return report
                    await resource()
                    net=len(current.ids)-len(previous.ids)
                    ready_growth=len(current.ready_ids)-len(previous.ready_ids)
                    newly_ready=set(current.ready_ids)-set(previous.ready_ids)
                    selected=len(newly_ready) if method.endswith('READY') else net
                    stalls=stalls+1 if selected<density.min_growth else 0
                    ever_ready=ever_ready or bool(current.ready_ids)
                    ever_listed=ever_listed or bool(current.ids)
                    report.trace.append({'batch':batch_index+1,'request_offset':allocated-load.allocated,
                        'listed':len(current.ids),'ready':len(current.ready_ids),'net_growth':net,
                        'ready_growth':len(newly_ready),'ready_net_growth':ready_growth,
                        'new_ready_ids':sorted(newly_ready),
                        'lost_ready_ids':sorted(set(previous.ready_ids)-set(current.ready_ids)),
                        'selected_growth':selected,'stall_batches':stalls,
                        'lost_ids':sorted(set(previous.ids)-set(current.ids)),
                        'new_ids':sorted(set(current.ids)-set(previous.ids)),
                        'completed_success':load.successes,'observed_at':self.clock()})
                    previous=current
                    if load.stop_reason!='budget_exhausted':
                        report.stop_reason=load.stop_reason;break
                    if stalls>=density.stall_limit:
                        created_any=ever_ready if method.endswith('READY') else ever_listed
                        report.stop_reason='plateau' if created_any else 'setup_failure'
                        report.normalized_stop_reason='plateau' if ever_ready else 'setup_failure'
                        break
                    if len(current.ids)>=density.max_instances:
                        report.stop_reason='instance_cap';break
                    if allocated>=case.load.requests:
                        report.stop_reason='request_budget';break
                    if batch_index+1>=density.max_batches:
                        report.stop_reason='batch_cap';break
                    await self.sleep(min(density.batch_gap_s,max(0,deadline-self.clock())))
                return report
            mode=case.load.mode
            load=await batch(case.load.requests,mode=mode)
            if method=='B09.PAUSED':
                await self._bounded(lambda:self.pause_cohort(tuple(sorted(known_created))),deadline)
            final=await inspect_cohort()
            if final is None:return report
            observed=await resource()
            report.stop_reason=load.stop_reason
            if method.startswith('B09.'):
                report.creation_throughput_status='not_applicable'
                required=set(final.paused_ids if method=='B09.PAUSED' else final.ready_ids)
                if len(known_created)!=case.load.requests or not known_created<=required:
                    return stop('invalid','prepared_cohort_not_in_required_state')
            if method=='B10.PROJECTION':
                baseline=report.baseline.get('mem_available_bytes')
                samples=observed.get('mem_available_samples',[observed.get('mem_available_bytes')])
                report.projection=projection(baseline,samples,case.load.requests,
                    len(known_created & set(final.ready_ids)),case.density.reserve_bytes,
                    domain=report.baseline['domain'],sample_domain=observed['domain'],
                    window=observed.get('window'),window_kind=observed.get('window_kind','declared_snapshot'),
                    window_version=observed.get('window_version'),
                    compatibility=observed.get('compatibility'))
            return report
        except TimeoutError:
            return stop('incomplete','deadline')
        except Exception as error:
            report.trace.append({'observation_error_type':type(error).__name__,'observed_at':self.clock()})
            return stop('insufficient','observation_or_permission_error')
