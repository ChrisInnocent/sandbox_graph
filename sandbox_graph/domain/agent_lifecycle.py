"""Versioned lifecycle wire contracts and a pure, evidence-guarded state machine.

This module performs no I/O, authorization lookup, budget reservation or dispatch.
Services must verify guards against their ledger within their fenced transaction.
"""
from __future__ import annotations

from types import MappingProxyType
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator

from sandbox_graph.domain.models import NodeResult


Ref = Annotated[str, StringConstraints(strict=True, min_length=1, pattern=r'^\S+$')]
Digest = Annotated[str, StringConstraints(strict=True, pattern=r'^[a-f0-9]{64}$')]
PositiveInt = Annotated[int, Field(strict=True, gt=0)]
Count = Annotated[int, Field(strict=True, ge=0)]
Instant = Annotated[float, Field(strict=True, gt=0, allow_inf_nan=False)]

Phase = Literal[
    'draining', 'reconciling', 'diagnosing', 'remediation_pending', 'remediating',
    'backoff', 'reentering', 'candidate_building', 'candidate_verifying',
    'promotion_pending', 'verified_waiting_worker', 'waiting_decision',
    'waiting_dependency', 'cancelling', 'blocked', 'fulfilled', 'superseded',
    'blocked_closed', 'cancelled', 'failed',
]
EventKind = Literal[
    'disposition_confirmed', 'effect_unknown', 'effect_known', 'observation_due',
    'budget_exhausted', 'known_action', 'needs_code', 'needs_input', 'no_safe_route',
    'claimed', 'postcheck_pass', 'postcheck_fail_with_budget',
    'postcheck_fail_exhausted', 'unknown', 'timer_due', 'node_accepted', 'node_failed',
    'revision_frozen', 'provider_unknown', 'tests_fail', 'tests_pass',
    'environment_missing', 'approved_and_ready', 'needs_authorization',
    'worker_missing', 'proof_invalid', 'nonruntime_candidate_accepted', 'ready',
    'proof_expired', 'valid_decision', 'dependency_ready', 'deadline', 'cancel',
    'owner_lost', 'irrecoverable_contract_error', 'disposition_closed',
    'authorized_resume', 'close_partial', 'version_drained',
]
StopReason = Literal['explicit_cancel', 'deadline', 'budget_exhausted']
TERMINAL_PHASES = frozenset({'fulfilled', 'superseded', 'blocked_closed', 'cancelled', 'failed'})
# Resume destinations are validated again according to the waiting phase below.
RESUMABLE_PHASES = frozenset({
    'draining', 'diagnosing', 'remediation_pending', 'remediating', 'backoff',
    'reentering', 'candidate_building', 'candidate_verifying', 'promotion_pending',
    'verified_waiting_worker', 'waiting_decision', 'waiting_dependency', 'cancelling',
})


class LifecycleModel(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True, strict=True, allow_inf_nan=False)
    schema_version: Literal[1]

    @field_validator('schema_version', mode='before')
    @classmethod
    def explicit_integer_version(cls, value):
        if type(value) is not int:
            raise ValueError('schema_version must be an integer')
        return value


class LifecycleKey(LifecycleModel):
    run_ref: Ref
    scope_kind: Literal['run', 'group', 'work', 'node']
    scope_ref: Ref
    node_id: Ref
    lifecycle_id: Ref
    lineage_budget_ref: Ref


class BackoffPolicy(LifecycleModel):
    seconds: Annotated[float, Field(strict=True, gt=0, allow_inf_nan=False)]
    unit: Literal['seconds']


class SafetyBudget(LifecycleModel):
    max_stop_calls: PositiveInt
    max_query_calls: PositiveInt
    max_cleanup_calls: PositiveInt
    cost_limit: Annotated[float, Field(strict=True, gt=0, allow_inf_nan=False)]
    cost_unit: Ref
    deadline: Instant
    clock: Literal['wall']


class RepairPolicy(LifecycleModel):
    policy_version: Ref
    actions: tuple[Ref, ...] = ()
    max_repair_attempts: PositiveInt
    max_diagnosis_calls: PositiveInt
    max_candidate_revisions: PositiveInt
    token_limit: PositiveInt
    token_unit: Literal['tokens']
    child_job_limit: PositiveInt
    max_depth: PositiveInt
    max_concurrency: PositiveInt
    deadline: Instant
    clock: Literal['wall']
    backoff: BackoffPolicy
    max_same_failure: PositiveInt
    max_observations: PositiveInt
    safety_budget: SafetyBudget
    postcheck_ref: Ref

    @model_validator(mode='after')
    def bounded_safety_and_unique_actions(self):
        if self.safety_budget.deadline < self.deadline:
            raise ValueError('safety deadline must cover the business deadline')
        if len(set(self.actions)) != len(self.actions):
            raise ValueError('action references must be unique')
        return self


class BudgetUsage(LifecycleModel):
    repair_attempts: Count = 0
    diagnosis_calls: Count = 0
    candidate_revisions: Count = 0
    tokens_reserved: Count = 0
    child_jobs: Count = 0
    observations: Count = 0
    stop_calls: Count = 0
    cleanup_calls: Count = 0


class ContextPacket(LifecycleModel):
    key: LifecycleKey
    node_supervisor_id: Ref
    task_role: Literal['diagnosis', 'repair', 'candidate_build', 'candidate_verify']
    graph_ref: Ref
    plan_ref: Ref
    contract_ref: Ref
    failure_summary: Annotated[str, Field(min_length=1)]
    facts: tuple[Annotated[str, Field(min_length=1)], ...]
    hypotheses: tuple[Annotated[str, Field(min_length=1)], ...]
    evidence_refs: tuple[Ref, ...]
    evidence_scope_refs: tuple[Ref, ...]
    evidence_byte_limit: PositiveInt
    budget_usage: BudgetUsage
    tool_capabilities: tuple[Ref, ...] = ()
    evidence_mode: Literal['live', 'offline_sealed'] = 'live'
    disposition_receipt_ref: Ref | None

    @model_validator(mode='after')
    def context_authority_mode(self):
        if self.evidence_mode == 'live' and self.disposition_receipt_ref is None:
            raise ValueError('live context requires a disposition receipt')
        if self.evidence_mode == 'offline_sealed':
            if self.disposition_receipt_ref is not None or self.tool_capabilities:
                raise ValueError('offline sealed context has no live disposition or tools')
        return self


class RepairRequest(LifecycleModel):
    request_id: Ref
    lifecycle_id: Ref
    source_receipt: Ref
    expected_revision: Count
    action_ref: Ref
    input_manifest_ref: Ref
    proposal_ref: Ref
    authorization_ref: Ref


class ResourceDispositionReceipt(LifecycleModel):
    work_ref: Ref
    operation_refs: tuple[Ref, ...]
    stopped: bool
    admission_status: Literal['open', 'closed']
    sampler_status: Literal['running', 'stopped', 'not_started', 'unknown']
    owned_resources: tuple[Ref, ...]
    residual_resources: tuple[Ref, ...]
    isolation_domains: tuple[Ref, ...]
    observed_at: Instant
    expires_at: Instant
    clock: Literal['wall']
    producer: Ref
    producer_version: Ref

    @model_validator(mode='after')
    def valid_interval(self):
        if self.expires_at <= self.observed_at:
            raise ValueError('disposition proof must expire after observation')
        return self


class RepairOutcome(LifecycleModel):
    order_ref: Ref
    operation_refs: tuple[Ref, ...]
    effect_certainty: Literal['none', 'confirmed', 'unknown']
    execution_status: Literal['pending', 'running', 'succeeded', 'failed', 'blocked', 'cancelled']
    data_status: Literal['unverified', 'valid', 'invalid', 'insufficient']
    cleanup_status: Literal['not_needed', 'pending', 'complete', 'incomplete', 'unknown']
    postcheck_ref: Ref
    raw_refs: tuple[Ref, ...]


class RepairPostcheckEvidence(LifecycleModel):
    """Historical independent check, never an execution error or new authority."""
    lifecycle_id: Ref
    order_ref: Ref
    action_ref: Ref
    postcheck_ref: Ref
    receipt_ref: Ref
    receipt_hash: Digest
    disposition_ref: Ref
    effect_certainty: Literal['none', 'confirmed', 'unknown']
    postcheck_status: Literal['accepted', 'rejected', 'unknown']
    producer: Literal['repair_outcome_service']
    producer_version: Literal['1']


class RecoveryPlan(LifecycleModel):
    ref: Ref
    graph_ref: Ref
    graph_hash: Digest
    contract_ref: Ref
    contract_hash: Digest
    input_hash: Digest
    work_revision: Count
    source_attempt: Ref
    entry_node: Ref
    revalidate_nodes: tuple[Ref, ...]
    reuse_receipts: tuple[Ref, ...]
    invalidated_refs: tuple[Ref, ...]
    resource_proofs: tuple[Ref, ...]
    measurement_generation: PositiveInt


class LifecycleEvent(LifecycleModel):
    event_id: Ref
    kind: EventKind
    source: Ref
    expected_revision: Count
    evidence_refs: Annotated[tuple[Ref, ...], Field(min_length=1)]
    observed_at: Instant
    resume_phase: Phase | None = None
    next_due_at: Instant | None = None
    operation_kind: Literal['site', 'provider'] | None = None
    operation_refs: tuple[Ref, ...] = ()
    isolation_refs: tuple[Ref, ...] = ()


class LifecycleRecord(LifecycleModel):
    key: LifecycleKey
    graph_ref: Ref
    plan_ref: Ref
    contract_ref: Ref
    policy_ref: Ref
    phase: Phase
    revision: Count
    owner_epoch: PositiveInt
    source_receipt: Ref
    issue_fingerprint: Digest
    deadline: Instant
    attempt_refs: tuple[Ref, ...] = ()
    decision_ref: Ref | None = None
    next_due_at: Instant | None = None
    stop_reason: StopReason | None = None
    terminal_intent: Literal['cancelled', 'blocked'] | None = None
    resume_phase: Phase | None = None
    operation_kind: Literal['site', 'provider'] | None = None
    operation_refs: tuple[Ref, ...] = ()
    isolation_refs: tuple[Ref, ...] = ()
    node_result: NodeResult = Field(default_factory=NodeResult)
    events: tuple[LifecycleEvent, ...] = ()

    @model_validator(mode='after')
    def atomic_stop_intent(self):
        if self.stop_reason is None:
            if self.terminal_intent is not None:
                raise ValueError('terminal_intent requires stop_reason')
        else:
            expected = 'cancelled' if self.stop_reason == 'explicit_cancel' else 'blocked'
            if self.terminal_intent != expected:
                raise ValueError('terminal_intent must match stop_reason')
        return self

    @property
    def is_terminal(self) -> bool:
        return self.phase in TERMINAL_PHASES


class TransitionGuards(LifecycleModel):
    """Verified facts from the service transaction, never model/provider claims."""
    work_lease_valid: bool = False
    attempt_closed: bool = False
    disposition_closed: bool = False
    effect_known: bool = False
    action_registered: bool = False
    authority_valid: bool = False
    budget_available: bool = False
    safety_budget_available: bool = False
    safety_deadline: Instant | None = None
    observation_budget_available: bool = False
    postcheck_verified: bool = False
    recovery_plan_valid: bool = False
    node_acceptance_valid: bool = False
    promotion_proof_valid: bool = False
    worker_ready: bool = False
    successor_bound: bool = False
    decision_valid: bool = False
    dependency_valid: bool = False
    proof_valid: bool = False
    resume_authorized: bool = False
    partial_close_authorized: bool = False
    effects_in_flight: bool = True
    same_failure_count: Count = 0
    max_same_failure: PositiveInt = 1
    new_evidence: bool = False


class InvalidLifecycleTransition(ValueError):
    """An unlisted event, conflicting replay or missing verified precondition."""


class StaleLifecycleEvent(InvalidLifecycleTransition):
    """Event expected_revision does not match the current record."""


# Only these phase-local edges exist. Global stop/fence events are handled below.
EVENT_TRANSITIONS = MappingProxyType({
    ('draining', 'disposition_confirmed'): 'diagnosing',
    ('draining', 'effect_unknown'): 'reconciling',
    ('reconciling', 'effect_known'): '@resume',
    ('reconciling', 'observation_due'): 'reconciling',
    ('diagnosing', 'known_action'): 'remediation_pending',
    ('diagnosing', 'needs_code'): 'candidate_building',
    ('diagnosing', 'needs_input'): 'waiting_decision',
    ('diagnosing', 'no_safe_route'): 'blocked',
    ('remediation_pending', 'claimed'): 'remediating',
    ('remediating', 'postcheck_pass'): 'reentering',
    ('remediating', 'postcheck_fail_with_budget'): '@retry',
    ('remediating', 'postcheck_fail_exhausted'): 'blocked',
    ('remediating', 'unknown'): 'reconciling',
    ('backoff', 'timer_due'): 'diagnosing',
    ('reentering', 'node_accepted'): 'fulfilled',
    ('reentering', 'node_failed'): 'draining',
    ('candidate_building', 'revision_frozen'): 'candidate_verifying',
    ('candidate_building', 'provider_unknown'): 'reconciling',
    ('candidate_verifying', 'tests_fail'): 'candidate_building',
    ('candidate_verifying', 'tests_pass'): 'promotion_pending',
    ('candidate_verifying', 'environment_missing'): 'waiting_dependency',
    ('promotion_pending', 'approved_and_ready'): 'superseded',
    ('promotion_pending', 'needs_authorization'): 'waiting_decision',
    ('promotion_pending', 'worker_missing'): 'verified_waiting_worker',
    ('promotion_pending', 'proof_invalid'): 'candidate_verifying',
    ('promotion_pending', 'nonruntime_candidate_accepted'): '@nonruntime',
    ('verified_waiting_worker', 'ready'): 'promotion_pending',
    ('verified_waiting_worker', 'proof_expired'): 'candidate_verifying',
    ('waiting_decision', 'valid_decision'): '@resume',
    ('waiting_dependency', 'dependency_ready'): '@resume',
    ('cancelling', 'disposition_closed'): '@terminal_intent',
    ('cancelling', 'unknown'): 'blocked',
    ('blocked', 'authorized_resume'): '@resume',
    ('blocked', 'close_partial'): 'blocked_closed',
})
_NEW_ACTION_EVENTS = frozenset({
    'known_action', 'needs_code', 'claimed', 'postcheck_fail_with_budget',
    'tests_fail', 'timer_due', 'approved_and_ready', 'ready', 'authorized_resume',
})


def transition_lifecycle(
    record: LifecycleRecord, event: LifecycleEvent, guards: TransitionGuards,
) -> LifecycleRecord:
    """Return a new revision or reject; exact event replay returns the same record.

    This is a local validator, not a substitute for transactional CAS, persistent
    event identity, lineage accounting, receipt provenance or work ownership.
    """
    def require(condition: bool, reason: str) -> None:
        if not condition:
            raise InvalidLifecycleTransition(reason)

    for saved in record.events:
        if saved.event_id == event.event_id:
            require(saved == event, 'event_id replay has different content')
            return record
    if event.expected_revision != record.revision:
        raise StaleLifecycleEvent('expected_revision does not match lifecycle revision')
    require(not record.is_terminal, 'terminal lifecycle does not accept new events')
    kind, phase = event.kind, record.phase
    changes: dict = {}

    def quarantine() -> None:
        refs = tuple(dict.fromkeys((*record.isolation_refs, *event.isolation_refs)))
        require(bool(refs), 'unknown effects require persistent isolation evidence')
        changes['isolation_refs'] = refs

    def stopping(reason: StopReason) -> str:
        require((event.operation_kind is None) == (not event.operation_refs),
                'stop event operation kind and refs must be supplied together')
        require((record.operation_kind is None) == (not record.operation_refs),
                'saved operation kind and refs must be bound together')
        if record.operation_refs and event.operation_refs:
            require((record.operation_kind, record.operation_refs)
                    == (event.operation_kind, event.operation_refs),
                    'stop event cannot replace the bound original operation')
        operation_kind = record.operation_kind or event.operation_kind
        operation_refs = record.operation_refs or event.operation_refs
        if guards.effects_in_flight or not guards.effect_known:
            require(operation_kind is not None and bool(operation_refs),
                    'inflight or unknown effects require bound original operation')
        if operation_refs:
            changes.update(operation_kind=operation_kind, operation_refs=operation_refs)
        # Explicit cancellation survives a later timeout; timeout beats budget.
        reasons = {'budget_exhausted': 0, 'deadline': 1, 'explicit_cancel': 2}
        saved = record.stop_reason
        if saved is not None and reasons[saved] > reasons[reason]:
            reason = saved
        changes['stop_reason'] = reason
        changes['terminal_intent'] = 'cancelled' if reason == 'explicit_cancel' else 'blocked'
        changes['resume_phase'] = record.resume_phase or phase
        if not guards.effect_known and guards.effects_in_flight:
            quarantine()
        if guards.disposition_closed and not guards.effects_in_flight:
            return 'cancelling' if kind == 'cancel' or reason == 'explicit_cancel' else 'blocked'
        if not guards.effect_known:
            quarantine()
        if (not guards.safety_budget_available or guards.safety_deadline is None
                or event.observed_at >= guards.safety_deadline):
            if guards.effects_in_flight:
                quarantine()
            return 'blocked'
        return 'cancelling' if kind == 'cancel' or guards.effect_known else 'reconciling'

    if kind in {'cancel', 'deadline', 'budget_exhausted'}:
        target = stopping('explicit_cancel' if kind == 'cancel' else kind)
    elif kind == 'version_drained':
        require(guards.work_lease_valid and guards.effect_known and guards.disposition_closed
                and not guards.effects_in_flight and guards.successor_bound,
                'drained version requires fenced closed disposition and verified successor')
        require(record.stop_reason != 'explicit_cancel', 'explicit cancel takes priority over version drain')
        target = 'superseded'
    elif kind == 'owner_lost':
        target = phase
    elif kind == 'irrecoverable_contract_error':
        require(guards.disposition_closed and not guards.effects_in_flight,
                'contract failure requires closed disposition')
        target = 'failed'
    else:
        target = EVENT_TRANSITIONS.get((phase, kind))
        require(target is not None, f'unlisted event {kind} in phase {phase}')
        require(guards.work_lease_valid, 'current work lease required')
        if kind in _NEW_ACTION_EVENTS:
            require(event.observed_at < record.deadline, 'deadline prohibits new work')
            require(record.terminal_intent is None or kind == 'authorized_resume',
                    'stop intent prohibits ordinary work')
        if kind == 'disposition_confirmed':
            require(guards.attempt_closed and guards.disposition_closed, 'attempt disposition required')
        elif kind in {'effect_unknown', 'unknown', 'provider_unknown'}:
            require(bool(event.operation_refs) and event.operation_kind is not None,
                    'unknown effects must identify original operation and kind')
            require(kind != 'provider_unknown' or event.operation_kind == 'provider',
                    'provider reconciliation cannot become a site operation')
            if phase == 'cancelling' and record.operation_refs:
                require((record.operation_kind, record.operation_refs)
                        == (event.operation_kind, event.operation_refs),
                        'unknown safety result cannot replace original operation')
            resume = record.resume_phase if phase == 'cancelling' else phase
            require(resume in RESUMABLE_PHASES, 'unknown effect requires recoverable original phase')
            require(event.resume_phase is None or event.resume_phase == resume,
                    'reconciliation cannot change original operation phase')
            quarantine()
            changes.update(operation_kind=event.operation_kind, operation_refs=event.operation_refs,
                           resume_phase=resume)
        elif kind == 'effect_known':
            require(guards.effect_known, 'operation effect not known')
        elif kind == 'observation_due':
            require(guards.observation_budget_available and guards.safety_budget_available,
                    'finite observation and safety allowance required')
            if record.terminal_intent is None:
                require(event.observed_at < record.deadline, 'business observation deadline expired')
            else:
                require(guards.safety_deadline is not None and event.observed_at < guards.safety_deadline,
                        'reserved safety observation deadline expired or unspecified')
        elif kind in {'known_action', 'needs_code', 'claimed'}:
            require(guards.authority_valid and guards.budget_available,
                    'authorized next action and budget required')
            require(guards.disposition_closed and guards.effect_known, 'live operation must be closed')
            if kind in {'known_action', 'claimed'}:
                require(guards.action_registered, 'registered action required')
            require(guards.same_failure_count < guards.max_same_failure or guards.new_evidence,
                    'same failure limit requires a different strategy or new evidence')
        elif kind in {'postcheck_pass', 'postcheck_fail_with_budget', 'postcheck_fail_exhausted'}:
            require(guards.postcheck_verified and guards.effect_known,
                    'independent postcheck and known effect required')
            if kind == 'postcheck_fail_with_budget':
                require(guards.budget_available, 'next repair budget exhausted')
                require(guards.same_failure_count < guards.max_same_failure or guards.new_evidence,
                        'repeated failure without progress')
                if event.next_due_at is not None:
                    require(event.observed_at < event.next_due_at < record.deadline,
                            'backoff must be within absolute deadline')
                    changes['next_due_at'] = event.next_due_at
            if kind == 'postcheck_fail_exhausted':
                target = stopping('budget_exhausted')
        elif kind == 'timer_due':
            require(record.next_due_at is not None and event.observed_at >= record.next_due_at,
                    'backoff timer not due')
        elif kind == 'node_accepted':
            require(guards.recovery_plan_valid and guards.node_acceptance_valid,
                    'recovery plan and new attempt acceptance required')
            require(guards.disposition_closed and not guards.effects_in_flight,
                    'fulfillment requires closed disposition')
        elif kind in {'revision_frozen', 'tests_pass', 'tests_fail'}:
            require(guards.proof_valid, 'verified candidate revision or gate evidence required')
            if kind == 'tests_fail':
                require(guards.budget_available, 'next candidate revision budget exhausted')
        elif kind == 'approved_and_ready':
            require(guards.authority_valid and guards.promotion_proof_valid and guards.worker_ready,
                    'promotion proof, authorization and worker required')
            require(guards.successor_bound and guards.disposition_closed and not guards.effects_in_flight,
                    'supersession requires successor and resource disposition')
        elif kind == 'nonruntime_candidate_accepted':
            require(guards.promotion_proof_valid and guards.authority_valid,
                    'nonruntime acceptance requires proof and authority')
        elif kind == 'ready':
            require(guards.worker_ready and guards.proof_valid, 'worker readiness and proof required')
        elif kind == 'valid_decision':
            require(guards.decision_valid and guards.authority_valid, 'identity-bound decision required')
        elif kind == 'dependency_ready':
            require(guards.dependency_valid, 'identity-bound dependency required')
        elif kind == 'disposition_closed':
            require(guards.disposition_closed and not guards.effects_in_flight,
                    'stop disposition has not closed')
        elif kind == 'authorized_resume':
            require(guards.resume_authorized and guards.authority_valid and guards.budget_available,
                    'explicit resume authorization and adjusted budget required')
            require(guards.effect_known and guards.disposition_closed,
                    'unresolved effects prohibit ordinary resume')
            changes.update(stop_reason=None, terminal_intent=None)
        elif kind == 'close_partial':
            require(guards.partial_close_authorized, 'explicit partial closure required')

        if target == '@resume':
            # Waits can only return to their saved identity-bound destination.
            target = record.resume_phase
            if phase == 'reconciling' and record.terminal_intent is not None:
                target = 'cancelling'
            require(target in RESUMABLE_PHASES, 'invalid or missing saved resume phase')
            if event.resume_phase is not None:
                require(event.resume_phase == target, 'event cannot overwrite saved resume phase')
            if phase == 'reconciling' and target != 'cancelling':
                require(guards.disposition_closed, 'isolation is not released')
        elif target == '@retry':
            target = 'backoff' if event.next_due_at is not None else 'diagnosing'
        elif target == '@nonruntime':
            require(event.resume_phase in {'diagnosing', 'blocked'}, 'nonruntime destination required')
            target = event.resume_phase
        elif target == '@terminal_intent':
            require(record.terminal_intent in {'cancelled', 'blocked'}, 'missing stop intent')
            target = record.terminal_intent
        if target in {'waiting_decision', 'waiting_dependency', 'verified_waiting_worker'}:
            require(event.resume_phase is None or event.resume_phase == phase,
                    'waiting must preserve the original phase')
            changes['resume_phase'] = phase

    if target == 'blocked' and 'resume_phase' not in changes and record.resume_phase is None:
        changes['resume_phase'] = phase
    changes.update(phase=target, revision=record.revision + 1, events=(*record.events, event))
    # Validate the resulting wire value instead of using an unchecked model_copy.
    return LifecycleRecord.model_validate({**record.model_dump(), **changes})
