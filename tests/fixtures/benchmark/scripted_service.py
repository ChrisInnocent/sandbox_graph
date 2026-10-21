"""Deterministic, injectable product stand-in for tester-contract tests.

A test scripts per-request outcomes (success/failed/unknown/timeout) and delays, so a
slow response, a lost reply or a partial failure can be reproduced without a real
product. It only wraps ``OperationResult``; it never runs a production algorithm to
produce expected values.
"""
from __future__ import annotations

from dataclasses import dataclass

from sandbox_bench.load import OperationResult


@dataclass(frozen=True)
class ScriptedOutcome:
    status: str
    delay_s: float = 0.0
    unresolved_external: int = 0
    value: dict | None = None


class ScriptedSandboxService:
    def __init__(self, outcomes, *, clock, sleep):
        self._outcomes = dict(outcomes)
        self._clock = clock
        self._sleep = sleep
        self.calls = []

    async def operation(self, index):
        self.calls.append(index)
        outcome = self._outcomes.get(index)
        if outcome is None:
            raise KeyError(f'no scripted outcome for request {index}')
        if outcome.delay_s:
            await self._sleep(outcome.delay_s)
        return OperationResult(status=outcome.status,
                               unresolved_external=outcome.unresolved_external,
                               value=outcome.value)
