"""Wire contracts. Frozen typed models reject unknown fields and non-finite values."""
from __future__ import annotations
import hashlib
import json
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator


def canonical_hash(value: Any) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode='json')
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode()).hexdigest()


class Model(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True, allow_inf_nan=False)
    schema_version: Literal[1] = 1


class TimePoint(Model):
    value: float
    clock: Literal['wall', 'monotonic']
    process_epoch: str | None = None

    @model_validator(mode='after')
    def clock_identity(self):
        if self.clock == 'monotonic' and not self.process_epoch:
            raise ValueError('monotonic clock requires process_epoch')
        return self


class WorkKey(Model):
    group_id: str = Field(min_length=1)
    scenario_id: str
    method_id: str
    method_version: str
    condition_hash: str = Field(pattern=r'^[a-f0-9]{64}$')
    repeat_id: int = Field(ge=1, strict=True)
    measurement_generation: int = Field(default=1, ge=1, strict=True)

    @property
    def identity(self) -> str:
        return canonical_hash(self)


class ErrorRecord(Model):
    code: str
    retry_class: Literal['never', 'bounded', 'reconcile'] = 'never'
    effect_certainty: Literal['none', 'confirmed', 'unknown'] = 'none'
    observed_at: float
    evidence_refs: tuple[str, ...] = ()
    message: str = ''


class NodeResult(Model):
    execution_status: Literal['pending', 'running', 'succeeded', 'failed', 'blocked', 'cancelled'] = 'pending'
    data_status: Literal['unverified', 'valid', 'invalid', 'insufficient'] = 'unverified'
    review_status: Literal['pending', 'accepted', 'rejected'] = 'pending'
    performance_status: Literal['not_evaluated', 'pass', 'fail', 'not_applicable'] = 'not_evaluated'
    cleanup_status: Literal['not_needed', 'pending', 'complete', 'incomplete', 'unknown'] = 'not_needed'
    coverage_status: Literal['selected', 'not_selected', 'unsupported', 'blocked', 'attempted', 'completed'] = 'selected'
    scope_disposition: Literal['fulfilled', 'failed', 'skipped', 'blocked_closed', 'cancelled'] | None = None
    assertions: tuple[dict[str, Any], ...] = ()
    artifact_refs: tuple[str, ...] = ()
    operation_refs: tuple[str, ...] = ()
    resource_refs: tuple[str, ...] = ()
    affected_scope: tuple[str, ...] = ()
    error: ErrorRecord | None = None
    continuation: str | None = None
    output: dict[str, Any] = Field(default_factory=dict)


class ScriptRef(Model):
    script_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    digest: str = Field(pattern=r'^[a-f0-9]{64}$')


class Operation(Model):
    operation_id: str = Field(min_length=1)
    input_hash: str = Field(pattern=r'^[a-f0-9]{64}$')
    script_ref: ScriptRef
    scope: str
    input: dict[str, Any] = Field(default_factory=dict)
    deadline: float
    phase: str = 'intent'
    node_id: str = 'N05'
    run_id: str = ''
    dispatch_generation: int = 1
    submission_kind: Literal['ordinary', 'drain', 'cleanup'] = 'ordinary'
    secret_refs: tuple[str, ...] = ()

    @model_validator(mode='after')
    def validate_request(self):
        if canonical_hash(self.input) != self.input_hash:
            raise ValueError('input_hash does not match structured input')
        if any(not ref.startswith('secret://') for ref in self.secret_refs):
            raise ValueError('credentials require secret:// references')
        return self


class EvidenceRef(Model):
    artifact_id: str
    uri: str
    digest: str = Field(pattern=r'^[a-f0-9]{64}$')
    size: int = Field(ge=0)
    media_type: str
    scope: str
    producer: str = 'unknown'
    source: str = 'unknown'
    time_domain: str = 'not_applicable'


class RunRecord(Model):
    run_id: str
    root_thread_id: str
    graph_ref: str
    contract_ref: str
    active_plan_ref: str
    auth_ref: str
    status: str = 'pending'
    revision: int = 0
    lineage_id: str
    supersedes_run_id: str | None = None
    successor_run_id: str | None = None
    dispatch_generation: int = 1


class NodeAttempt(Model):
    thread_id: str
    node_id: str
    visit_id: str
    attempt_id: str
    owner_agent_id: str
    owner_epoch: int = Field(ge=1)
    input_hash: str
    revision: int = 0
    continuation_of: str | None = None


class WorkInstance(Model):
    work_id: str
    key: WorkKey
    run_ref: str
    plan_ref: str
    environment_ref: str
    phase: str = 'pending'
    attempt_refs: tuple[str, ...] = ()
    reuse_from: str | None = None


class DecisionRecord(Model):
    decision_id: str
    question_id: str
    scope: str
    plan_ref: str
    contract_ref: str
    actor: str
    answer: dict[str, Any]
    expiry: float


class DeliveryManifest(Model):
    delivery_id: str
    version: int = Field(ge=1)
    scope: str
    plan_ref: str
    contract_ref: str
    work_refs: tuple[str, ...]
    evidence: tuple[EvidenceRef, ...]
    result: NodeResult
    gaps: tuple[str, ...] = ()
