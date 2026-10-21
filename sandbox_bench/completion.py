"""Read-only completion observation; post-validation never rewrites performance facts.

All callback arrivals are measured on one injected client monotonic clock and epoch.
Server timestamps/durations remain separate evidence. Callers supply an inspect function
that is genuinely read-only; auto-resume/connect endpoints are not inspect operations.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import math
import time
from typing import Any, Awaitable, Callable


@dataclass(frozen=True)
class CompletionSpec:
    kind: str
    timeout: float
    poll_interval: float
    ready_states: tuple[str, ...] = ('running',)
    failure_states: tuple[str, ...] = ('failed', 'error', 'terminated')

    def __post_init__(self) -> None:
        if self.kind not in {'API_COMPLETE', 'STATE_READY', 'SERVER_TERMINAL', 'EXEC_COMPLETE'}:
            raise ValueError('unknown completion kind')
        for name in ('timeout', 'poll_interval'):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or value <= 0:
                raise ValueError(f'{name} must be finite and positive')
        if not self.ready_states or set(self.ready_states) & set(self.failure_states):
            raise ValueError('ready and failure states must be nonempty/disjoint')
        if 'unknown' in self.ready_states:
            raise ValueError('unknown cannot be a successful state')


@dataclass(frozen=True)
class Acceptance:
    success: bool
    observed_at: float
    process_epoch: str
    handle: Any = None
    raw_ref: str | None = None


@dataclass(frozen=True)
class StateObservation:
    state: str
    process_epoch: str
    raw_ref: str | None = None
    source_event_time: Any = None


@dataclass(frozen=True)
class TerminalObservation:
    success: bool
    process_epoch: str
    internal_duration_ns: int | None = None
    source_event_time: Any = None
    raw_ref: str | None = None

    def __post_init__(self) -> None:
        value = self.internal_duration_ns
        if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
            raise ValueError('internal duration must be nonnegative integer nanoseconds')


@dataclass(frozen=True)
class PollEvidence:
    started_at: float
    observed_at: float
    state: str
    process_epoch: str
    raw_ref: str | None = None
    source_event_time: Any = None


@dataclass(frozen=True)
class CompletionResult:
    kind: str
    status: str
    submitted_at: float
    observed_at: float
    deadline: float
    process_epoch: str
    latency: float | None
    reason: str | None = None
    polls: tuple[PollEvidence, ...] = ()
    internal_duration_ns: int | None = None
    source_event_time: Any = None
    raw_ref: str | None = None
    ready_states: tuple[str, ...] = ('running',)
    submitted_epoch: str | None = None

    @property
    def unresolved_external(self) -> int:
        return int(self.status in {'timeout', 'unknown'})


@dataclass(frozen=True)
class LateCompletion:
    status: str
    observed_at: float
    process_epoch: str
    original_status: str
    raw_ref: str | None
    source_event_time: Any


@dataclass(frozen=True)
class ValidationResult:
    success: bool
    observed_at: float
    reason: str | None = None


class CompletionObserver:
    def __init__(self, *, process_epoch: str,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep):
        if not process_epoch:
            raise ValueError('process_epoch is required')
        self.process_epoch, self.clock, self.sleep = process_epoch, clock, sleep

    async def _bounded(self, callback: Callable[[], Awaitable[Any]], deadline: float) -> Any:
        remaining = deadline - self.clock()
        if remaining <= 0:
            raise TimeoutError
        task = asyncio.create_task(callback())
        timer = asyncio.create_task(self.sleep(remaining))
        try:
            done, _ = await asyncio.wait((task, timer), return_when=asyncio.FIRST_COMPLETED)
            if task in done and self.clock() < deadline:
                return task.result()
            raise TimeoutError
        finally:
            task.cancel()
            timer.cancel()
            await asyncio.gather(task, timer, return_exceptions=True)

    async def observe(self, spec: CompletionSpec, *, submitted_at: float,
                      process_epoch: str, accepted: Acceptance,
                      inspect: Callable[[Any], Awaitable[StateObservation]] | None = None,
                      execute: Callable[[Any], Awaitable[TerminalObservation]] | None = None,
                      server_terminal: Callable[[Any], Awaitable[TerminalObservation]] | None = None) -> CompletionResult:
        deadline = submitted_at + spec.timeout
        polls: list[PollEvidence] = []

        def finish(status: str, reason: str | None = None, *, at: float | None = None,
                   terminal: TerminalObservation | None = None) -> CompletionResult:
            observed = self.clock() if at is None else at
            return CompletionResult(spec.kind, status, submitted_at, observed, deadline,
                self.process_epoch, observed - submitted_at if status == 'success' else None,
                reason, tuple(polls),
                terminal.internal_duration_ns if terminal and status == 'success' and spec.kind == 'SERVER_TERMINAL' else None,
                terminal.source_event_time if terminal else None,
                terminal.raw_ref if terminal else accepted.raw_ref, spec.ready_states, process_epoch)

        # Compare epoch identities before comparing or subtracting any timestamps.
        if process_epoch != self.process_epoch or accepted.process_epoch != self.process_epoch:
            return finish('unknown', 'cross_epoch')
        if not math.isfinite(submitted_at) or not math.isfinite(accepted.observed_at):
            raise ValueError('monotonic timestamps must be finite')
        if accepted.observed_at < submitted_at or self.clock() < accepted.observed_at:
            return finish('unknown', 'invalid_clock_order')
        if not accepted.success:
            return finish('failed', 'api_rejected', at=accepted.observed_at)
        if accepted.observed_at >= deadline:
            return finish('timeout', 'deadline', at=deadline)
        if spec.kind == 'API_COMPLETE':
            return finish('success', at=accepted.observed_at)
        if self.clock() >= deadline:
            return finish('timeout', 'deadline', at=deadline)
        if spec.kind != 'STATE_READY':
            callback = execute if spec.kind == 'EXEC_COMPLETE' else server_terminal
            if callback is None:
                raise ValueError(f'{spec.kind} requires its explicit completion callback')
            try:
                terminal = await self._bounded(lambda: callback(accepted.handle), deadline)
            except TimeoutError:
                return finish('timeout', 'deadline', at=deadline)
            except Exception:
                return finish('unknown', 'observation_error')
            if not isinstance(terminal, TerminalObservation):
                return finish('unknown', 'invalid_terminal_observation')
            if terminal.process_epoch != self.process_epoch:
                return finish('unknown', 'cross_epoch')
            return finish('success' if terminal.success else 'failed', terminal=terminal)
        if inspect is None:
            raise ValueError('STATE_READY requires a read-only inspect callback')
        while self.clock() < deadline:
            began = self.clock()
            try:
                observation = await self._bounded(lambda: inspect(accepted.handle), deadline)
            except TimeoutError:
                return finish('timeout', 'deadline', at=deadline)
            except Exception:
                # Failed reads are evidence of an unknown state, never a stopped sandbox.
                observation = StateObservation('unknown', self.process_epoch)
            if not isinstance(observation, StateObservation):
                return finish('unknown', 'invalid_state_observation')
            polls.append(PollEvidence(began, self.clock(), observation.state,
                observation.process_epoch, observation.raw_ref, observation.source_event_time))
            if observation.process_epoch != self.process_epoch:
                return finish('unknown', 'cross_epoch')
            if observation.state in spec.ready_states:
                return finish('success')
            if observation.state in spec.failure_states:
                return finish('failed', 'terminal_state')
            # One cadence across products. Slow reads do not cause a catch-up burst.
            wake = min(deadline, max(began + spec.poll_interval, self.clock()))
            await self.sleep(max(0, wake - self.clock()))
        return finish('timeout', 'deadline', at=deadline)

    async def validate(self, validator: Callable[[], Awaitable[bool]], *, timeout: float = 30) -> ValidationResult:
        if isinstance(timeout, bool) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError('validation timeout must be finite and positive')
        try:
            success = await self._bounded(validator, self.clock() + timeout)
            if not isinstance(success, bool):
                return ValidationResult(False, self.clock(), 'invalid_assertion_result')
            return ValidationResult(success, self.clock(), None if success else 'assertion_failed')
        except TimeoutError:
            return ValidationResult(False, self.clock(), 'deadline')
        except Exception:
            return ValidationResult(False, self.clock(), 'validation_error')

    def record_late(self, original: CompletionResult,
                    observation: StateObservation | TerminalObservation) -> LateCompletion:
        if original.status not in {'timeout', 'unknown'}:
            raise ValueError('late evidence requires an unresolved original result')
        success = (observation.state in original.ready_states if isinstance(observation, StateObservation)
                   else observation.success)
        return LateCompletion('late_success' if success else 'late_non_success', self.clock(),
                              observation.process_epoch, original.status,
                              observation.raw_ref, observation.source_event_time)
