"""Structured control-plane trace (D16/D18.7): phase timings, usage, reuse counts.

Control-plane observability is kept separate from sandbox throughput. Secrets appear
only as references and raw logs are stored by reference, never inline. Missing token
usage is ``unknown`` -- never zero, never invented.
"""
from __future__ import annotations

from typing import Literal

from pydantic import Field

from ..domain.models import Model, canonical_hash
from ..persistence.repository import Conflict

Phase = Literal['queue', 'script', 'wait', 'reconcile', 'analysis', 'model']
REFERENCE_SCHEMES = ('artifact://', 'secret://', 'log://')


class TraceEvent(Model):
    trace_ref: str
    run_ref: str
    plan_ref: str
    graph_ref: str
    scope: str
    node_id: str
    attempt_id: str
    owner_epoch: int = Field(ge=1)
    operation_id: str | None = None
    phase: Phase
    started_at: float
    ended_at: float
    status: str
    usage_tokens: int | None = None
    usage_status: Literal['known', 'unknown'] = 'unknown'
    log_ref: str | None = None


def _require_reference(value: str, what: str) -> str:
    if not isinstance(value, str) or not value.startswith(REFERENCE_SCHEMES):
        raise ValueError(f'{what} must be stored by reference ({"/".join(REFERENCE_SCHEMES)})')
    return value


class ObservabilityService:
    def __init__(self, repository):
        self.repository = repository

    def record(self, event: TraceEvent) -> str:
        if event.ended_at < event.started_at:
            raise ValueError('negative phase duration is not a valid observation')
        if event.usage_status == 'known' and not isinstance(event.usage_tokens, int):
            raise ValueError('known usage requires an integer token count')
        if event.usage_status == 'unknown' and event.usage_tokens is not None:
            raise ValueError('unknown usage must not carry a fabricated token count')
        if event.log_ref is not None:
            _require_reference(event.log_ref, 'log_ref')
        body = event.model_dump(mode='json') | {'duration_s': event.ended_at - event.started_at}
        identity = canonical_hash(body)
        with self.repository.transaction() as tx:
            old = tx.get('traces', identity)
            if old and old['body'] != body:
                raise Conflict('trace identity conflict')
            tx.put('traces', identity, body, immutable=True)
        return identity

    def summary(self, run_ref: str) -> dict:
        with self.repository.transaction() as tx:
            rows = [row['body'] for row in tx.scan('traces') if row['body']['run_ref'] == run_ref]
            reuse = sum(1 for row in tx.scan('work_instances')
                        if row['body'].get('run_ref') == run_ref and row['body'].get('reuse_from'))
        phases: dict[str, dict[str, float]] = {}
        for row in rows:
            bucket = phases.setdefault(row['phase'], {'count': 0, 'duration_s': 0.0})
            bucket['count'] += 1
            bucket['duration_s'] += row['duration_s']
        return {'formula_version': 'observability.v1', 'run_ref': run_ref, 'phases': phases,
                'usage': {'known': sum(r['usage_status'] == 'known' for r in rows),
                          'unknown': sum(r['usage_status'] == 'unknown' for r in rows)},
                'reuse_count': reuse}


def store_raw_log(log_ref: str) -> str:
    """Raw orchestration logs live behind a reference, never inline in control state."""
    return _require_reference(log_ref, 'log_ref')
