"""Finite asynchronous load schedules with explicit durable admission.

The operation callable must cover submission *and the selected completion boundary*.
Returning merely an HTTP 201 is not a STATE_READY operation. The journal is mandatory;
this module never substitutes an in-memory ledger for durable submission permission.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
import math
import time
from typing import Any, Awaitable, Callable, Protocol


def _positive_int(value: int, name: str, *, zero: bool = False) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < (0 if zero else 1):
        raise ValueError(f'{name} must be a {"nonnegative" if zero else "positive"} integer')


def _seconds(value: float, name: str, *, positive: bool = False) -> None:
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or value < 0 or (positive and value == 0):
        raise ValueError(f'{name} must be finite and {"positive" if positive else "nonnegative"}')


@dataclass(frozen=True)
class LoadSchedule:
    mode: str
    request_count: int
    concurrency: int
    operation_timeout: float
    batch_size: int | None = None
    gap: float = 0
    scheduled_at: tuple[float, ...] = ()
    late_policy: str = 'drop'
    max_queue: int = 0
    unknown_limit: int = 1
    deadline: float | None = None  # relative to run start; active calls have bounded drain
    child_weights: tuple[int, ...] = ()
    child_budget: int | None = None
    parent_budget: int | None = None

    def __post_init__(self) -> None:
        if self.mode not in {'WINDOW', 'BATCH', 'OPEN_LOOP'}:
            raise ValueError('unknown schedule mode')
        _positive_int(self.request_count, 'request_count')
        _positive_int(self.concurrency, 'concurrency')
        _positive_int(self.unknown_limit, 'unknown_limit', zero=True)
        _positive_int(self.max_queue, 'max_queue', zero=True)
        _seconds(self.operation_timeout, 'operation_timeout', positive=True)
        _seconds(self.gap, 'gap')
        if self.deadline is not None:
            _seconds(self.deadline, 'deadline', positive=True)
        if self.mode == 'BATCH':
            _positive_int(self.batch_size, 'batch_size')
        elif self.batch_size is not None or self.gap:
            raise ValueError('batch parameters require BATCH')
        if self.late_policy not in {'drop', 'queue'}:
            raise ValueError('late_policy must be drop or queue')
        if self.mode == 'OPEN_LOOP':
            if len(self.scheduled_at) != self.request_count:
                raise ValueError('OPEN_LOOP requires one finite scheduled_at per request')
            for at in self.scheduled_at:
                _seconds(at, 'scheduled_at')
            if tuple(sorted(self.scheduled_at)) != tuple(self.scheduled_at):
                raise ValueError('scheduled_at must be nondecreasing')
            if self.late_policy == 'queue' and not self.max_queue:
                raise ValueError('queue requires a positive max_queue')
        elif self.scheduled_at or self.max_queue or self.late_policy != 'drop':
            raise ValueError('open-loop parameters require OPEN_LOOP')
        if self.child_weights and len(self.child_weights) != self.request_count:
            raise ValueError('one child_weight per parent request is required')
        for weight in self.weights:
            _positive_int(weight, 'child_weight')
            if weight > self.concurrency:
                raise ValueError('native fork child weight exceeds concurrency; splitting forbidden')
        if self.child_budget is not None:
            _positive_int(self.child_budget, 'child_budget')
            if sum(self.weights) > self.child_budget:
                raise ValueError('insufficient child budget for the authorized plan')
        if self.parent_budget is not None:
            _positive_int(self.parent_budget, 'parent_budget')
            if self.request_count > self.parent_budget:
                raise ValueError('insufficient parent request budget')

    @property
    def weights(self) -> tuple[int, ...]:
        return tuple(self.child_weights) or (1,) * self.request_count


@dataclass(frozen=True)
class OperationResult:
    status: str
    unresolved_external: int = 0
    reason: str | None = None
    value: Any = None

    def __post_init__(self) -> None:
        if self.status not in {'success', 'failed', 'timeout', 'unknown', 'unsent'}:
            raise ValueError('unknown operation result status')
        _positive_int(self.unresolved_external, 'unresolved_external', zero=True)


class SubmissionJournal(Protocol):
    """Implement using the owning runner's persistent, fenced operation ledger.

    reserve atomically consumes child_weight child slots and one parent request slot.
    It must reject an exhausted budget, reused operation, or lost ownership. A failure
    must not restore allocated budget automatically. No method here authorizes replay.
    """
    async def reserve(self, request_index: int, child_weight: int) -> Any: ...
    async def submission_started(self, token: Any) -> None: ...
    async def record_terminal(self, token: Any, result: OperationResult) -> None: ...


@dataclass(frozen=True)
class LoadEvent:
    kind: str
    at: float
    request_index: int | None
    active_slots: int
    queued: int = 0
    scheduled_at: float | None = None
    generator_lag: float | None = None
    refill_gap: float | None = None
    completed_at: float | None = None
    status: str | None = None
    child_index: int | None = None


@dataclass
class LoadReport:
    started_at: float
    finished_at: float
    events: list[LoadEvent] = field(default_factory=list)
    results: dict[int, OperationResult] = field(default_factory=dict)
    allocated: int = 0
    submitted: int = 0
    dropped: int = 0
    max_active_slots: int = 0
    max_queued: int = 0
    unresolved_external: int = 0
    stop_reason: str = 'budget_exhausted'
    target_rate_achieved: bool | None = None

    @property
    def successes(self) -> int:
        return sum(result.status == 'success' for result in self.results.values())

    @property
    def wall_seconds(self) -> float:
        return self.finished_at - self.started_at


class LoadEngine:
    def __init__(self, *, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep):
        self.clock, self.sleep = clock, sleep

    async def run(self, schedule: LoadSchedule,
                  operation: Callable[[int], Awaitable[OperationResult]], *,
                  journal: SubmissionJournal,
                  progress_operation: Callable[..., Awaitable[OperationResult]] | None = None) -> LoadReport:
        if progress_operation is not None and not callable(getattr(journal, "record_child_terminal", None)):
            raise ValueError("progress operation requires durable child terminal journal")
        start = self.clock()
        report = LoadReport(start, start)
        active: dict[int, asyncio.Task] = {}
        work_tasks: dict[int, asyncio.Task] = {}
        completion_times: dict[int, float] = {}
        remaining_weights: dict[int, int] = {}
        progress_unknown: dict[int, int] = {}
        progress_wakeup = asyncio.Event()
        queued: list[int] = []
        active_slots = 0
        next_index = 0
        batch_end = min(schedule.batch_size or schedule.request_count, schedule.request_count)
        weights = schedule.weights
        stopped = False
        last_release: float | None = None
        global_deadline = start + schedule.deadline if schedule.deadline is not None else None

        def emit(kind: str, index: int | None = None, **kwargs: Any) -> None:
            report.events.append(LoadEvent(kind, self.clock(), index, active_slots, len(queued), **kwargs))

        async def invoke(index: int, scheduled: float, released_at: float | None) -> OperationResult:
            token = None
            sent = False
            phase = 'reserve'
            child_seen: set[int] = set()
            child_results: dict[int, OperationResult] = {}
            channel_open = True
            submitted_at = None
            async def child_terminal(child_index: int, result: OperationResult, completed_at: float | None = None):
                nonlocal active_slots, last_release
                if not channel_open or submitted_at is None:
                    raise ValueError('child terminal outside active submission')
                if type(child_index) is not int or not 0 <= child_index < weights[index]:
                    raise ValueError('child terminal index outside reserved weight')
                if child_index in child_seen:
                    raise ValueError('duplicate or overlapping child terminal')
                if not isinstance(result, OperationResult) or result.unresolved_external > 1:
                    raise ValueError('child terminal must describe one reserved child')
                at = self.clock() if completed_at is None else completed_at
                _seconds(at, 'completed_at')
                if at < submitted_at or at > self.clock():
                    raise ValueError('child completion outside submission clock interval')
                child_seen.add(child_index)
                if result.status in {'timeout', 'unknown'} and not result.unresolved_external:
                    result = replace(result, unresolved_external=1)
                try:
                    await journal.record_child_terminal(token, child_index, result, at)
                except Exception:
                    report.stop_reason = 'journal_error'
                    progress_wakeup.set()
                    raise
                child_results[child_index] = result
                remaining_weights[index] -= 1
                active_slots -= 1
                progress_unknown[index] += result.unresolved_external
                report.unresolved_external += result.unresolved_external
                last_release = max(last_release or start, at)
                emit('child_completed', index, child_index=child_index, status=result.status, completed_at=at)
                progress_wakeup.set()

            async def work() -> OperationResult:
                nonlocal token, sent, phase, submitted_at
                token = await journal.reserve(index, weights[index])
                report.allocated += 1
                phase = 'permission'
                await journal.submission_started(token)
                phase = 'operation'
                # This assignment and the actual call have no intervening await.
                sent = True
                report.submitted += 1
                submitted_at = self.clock()
                emit('submit', index, scheduled_at=scheduled,
                     generator_lag=self.clock() - scheduled,
                     refill_gap=None if released_at is None else self.clock() - released_at)
                result = await progress_operation(index, child_terminal) if progress_operation is not None else await operation(index)
                if not isinstance(result, OperationResult):
                    raise TypeError('operation must return OperationResult')
                if result.unresolved_external > remaining_weights[index] + progress_unknown[index]:
                    raise ValueError('external unknown count exceeds unresolved reserved child weight')
                if result.status in {'timeout', 'unknown'} and not result.unresolved_external:
                    result = replace(result, unresolved_external=remaining_weights[index]+progress_unknown[index])
                if result.unresolved_external < progress_unknown[index]:
                    raise ValueError('parent result contradicts durable child unknowns')
                if result.status == 'success' and any(r.status != 'success' for r in child_results.values()):
                    raise ValueError('parent success contradicts failed child terminal')
                return result
            expires = self.clock() + schedule.operation_timeout
            task = asyncio.create_task(work())
            work_tasks[index] = task
            timer = asyncio.create_task(self.sleep(schedule.operation_timeout))
            try:
                done, _ = await asyncio.wait((task, timer), return_when=asyncio.FIRST_COMPLETED)
                if task in done and self.clock() < expires:
                    try:
                        result = task.result()
                    except Exception as error:
                        result = OperationResult('unknown' if sent else 'unsent',
                                                 remaining_weights[index]+progress_unknown[index] if sent else 0,
                                                 'operation_error' if sent else 'journal_error',
                                                 type(error).__name__)
                else:
                    result = OperationResult('timeout' if sent else 'unsent',
                                             remaining_weights[index]+progress_unknown[index] if sent else 0,
                                             'deadline' if sent else 'journal_error')
            finally:
                channel_open = False
                task.cancel()
                timer.cancel()
                await asyncio.gather(task, timer, return_exceptions=True)
            completion_times[index] = self.clock()
            if token is not None:
                # Persistence failures stop subsequent dispatch. An accepted result
                # is kept as a fact, but must not authorize another request.
                persistence = asyncio.create_task(journal.record_terminal(token, result))
                persist_timer = asyncio.create_task(self.sleep(schedule.operation_timeout))
                try:
                    done, _ = await asyncio.wait((persistence, persist_timer), return_when=asyncio.FIRST_COMPLETED)
                    if persistence not in done:
                        raise TimeoutError('terminal persistence deadline')
                    persistence.result()
                except Exception:
                    report.stop_reason = 'journal_error'
                finally:
                    persistence.cancel()
                    persist_timer.cancel()
                    await asyncio.gather(persistence, persist_timer, return_exceptions=True)
            if result.reason == 'journal_error':
                report.stop_reason = 'journal_error'
            return result

        def launch(index: int) -> None:
            nonlocal active_slots
            active_slots += weights[index]
            remaining_weights[index] = weights[index]
            progress_unknown[index] = 0
            report.max_active_slots = max(report.max_active_slots, active_slots)
            scheduled = start + schedule.scheduled_at[index] if schedule.mode == 'OPEN_LOOP' else self.clock()
            emit('admitted', index, scheduled_at=scheduled)
            active[index] = asyncio.create_task(invoke(index, scheduled, last_release))

        async def wait_until(at: float | None = None) -> None:
            timer = asyncio.create_task(self.sleep(max(0, at - self.clock()))) if at is not None else None
            child_wake = asyncio.create_task(progress_wakeup.wait())
            waiters = list(active.values()) + ([timer] if timer else []) + [child_wake]
            if not waiters:
                return
            try:
                await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
            finally:
                child_wake.cancel()
                if timer:timer.cancel()
                await asyncio.gather(child_wake, *([timer] if timer else []), return_exceptions=True)

        try:
            while True:
                progress_wakeup.clear()
                # A due arrival and completion can wake the loop together. Finish
                # durable terminal recording for already completed calls before
                # processing that arrival, independent of callback queue order.
                for index in sorted(active):
                    if index in work_tasks and work_tasks[index].done():
                        await active[index]
                for index in sorted(i for i, task in active.items() if task.done()):
                    result = active.pop(index).result()
                    report.results[index] = result
                    report.unresolved_external += result.unresolved_external - progress_unknown[index]
                    active_slots -= remaining_weights[index]
                    if remaining_weights[index]:
                        last_release = max(last_release or start, completion_times[index])
                    emit('completed', index, status=result.status, completed_at=completion_times[index])
                if not stopped:
                    if report.stop_reason == 'journal_error':
                        stopped = True
                    elif report.unresolved_external and report.unresolved_external >= schedule.unknown_limit:
                        stopped, report.stop_reason = True, 'unknown_limit'
                    elif global_deadline is not None and self.clock() >= global_deadline:
                        stopped, report.stop_reason = True, 'deadline'
                    if stopped:
                        emit('stop')
                        queued.clear()
                if stopped:
                    if not active:
                        break
                    await wait_until()
                    continue

                if schedule.mode == 'OPEN_LOOP':
                    # Existing FIFO arrivals get capacity before arrivals at this tick.
                    while queued and active_slots + weights[queued[0]] <= schedule.concurrency:
                        launch(queued.pop(0))
                    while next_index < schedule.request_count and start + schedule.scheduled_at[next_index] <= self.clock():
                        index = next_index
                        next_index += 1
                        emit('scheduled', index, scheduled_at=start + schedule.scheduled_at[index])
                        if not queued and active_slots + weights[index] <= schedule.concurrency:
                            launch(index)
                        elif schedule.late_policy == 'queue' and len(queued) < schedule.max_queue:
                            queued.append(index)
                            report.max_queued = max(report.max_queued, len(queued))
                            emit('queued', index)
                        else:
                            report.dropped += 1
                            emit('dropped', index, status='unsent')
                else:
                    limit = batch_end if schedule.mode == 'BATCH' else schedule.request_count
                    while next_index < limit and active_slots + weights[next_index] <= schedule.concurrency:
                        launch(next_index)
                        next_index += 1
                    if schedule.mode == 'BATCH' and not active and next_index == batch_end and next_index < schedule.request_count:
                        emit('gap')
                        target = self.clock() + schedule.gap
                        await self.sleep(max(0, min(target, global_deadline) - self.clock()) if global_deadline is not None else schedule.gap)
                        batch_end = min(batch_end + schedule.batch_size, schedule.request_count)
                        continue
                if next_index == schedule.request_count and not active and not queued:
                    break
                wake = global_deadline
                if schedule.mode == 'OPEN_LOOP' and next_index < schedule.request_count:
                    arrival = start + schedule.scheduled_at[next_index]
                    wake = min(wake, arrival) if wake is not None else arrival
                await wait_until(wake)
        finally:
            for task in active.values():
                task.cancel()
            await asyncio.gather(*active.values(), return_exceptions=True)
        report.finished_at = self.clock()
        if schedule.mode == 'OPEN_LOOP':
            report.target_rate_achieved = (
                report.submitted == schedule.request_count and report.dropped == 0
                and all(e.generator_lag == 0 for e in report.events if e.kind == 'submit'))
        return report
