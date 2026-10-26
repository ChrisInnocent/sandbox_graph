"""Versioned stop intent/projection/proof contracts; no inferred authority."""
from typing import Literal
from pydantic import Field, model_validator
from .agent_lifecycle import LifecycleModel, Ref, Digest, Count, PositiveInt, Instant


class StopCommand(LifecycleModel):
    request_id: Ref
    run_ref: Ref
    plan_ref: Ref
    graph_ref: Ref
    reason: str = Field(min_length=1,max_length=4096)
    policy: Literal['cancel','drain']
    cutoff_generation: PositiveInt
    cutoff_disposition_revision: Count
    created_at: Instant
    producer: Literal['run_control.v1']


class StopTarget(StopCommand):
    command_ref: Ref
    lifecycle_id: Ref
    scope_kind: Literal['run','group','work','node']
    scope_ref: Ref
    observed_revision: Count
    observed_phase: Ref


class StopStatus(LifecycleModel):
    status: Literal['pending','waiting_dependency','closed']
    request_ref: Ref | None = None
    lifecycle_id: Ref | None = None
    proof_ref: Ref | None = None
    reason: str | None = None
    recovery_entry: str | None = None
    remaining: dict[str,list[str]] = Field(default_factory=dict)
    disposition_revision: Count | None = None
    due_at: Instant | None = None
    stop_job_ref: Ref | None = None
    poll_budget_exhausted: bool = False

    @model_validator(mode='after')
    def closure_requires_proof(self):
        if self.status=='closed' and (self.proof_ref is None or self.lifecycle_id is None):
            raise ValueError('closed stop projection requires original lifecycle proof')
        return self


class StopProof(LifecycleModel):
    proof_ref: Ref
    request_ref: Ref
    lifecycle_id: Ref
    attempt_revision: Count
    inventory: dict[str,list[dict]]
    inventory_hash: Digest
    observed_at: Instant
    owner_epoch: PositiveInt
    lease_id: Ref
    thread_id: Ref
    successor_ref: Ref | None
    disposition_revision: Count
    inventory_digest: Digest
    command_hash: Digest
    producer: Literal['lifecycle_stop.v1']
