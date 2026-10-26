"""ALU-04/06/20: literal state oracle; no dispatcher or external effects."""
import pytest

from sandbox_graph.domain.agent_lifecycle import (
    LifecycleEvent, LifecycleKey, LifecycleRecord, TransitionGuards,
    InvalidLifecycleTransition, StaleLifecycleEvent, transition_lifecycle,
)
from sandbox_graph.domain.models import NodeResult


def record(phase='diagnosing', **changes):
    data = dict(schema_version=1, key=LifecycleKey(schema_version=1, run_ref='run/a',
        scope_kind='work', scope_ref='work/a', node_id='N02', lifecycle_id='life/a',
        lineage_budget_ref='lineage/a'), graph_ref='graph/v1', plan_ref='plan/v1',
        contract_ref='contract/v1', policy_ref='policy/v1', phase=phase, revision=0,
        owner_epoch=1, source_receipt='receipt/a', issue_fingerprint='a'*64, deadline=100.0,
        node_result=NodeResult(execution_status='failed', data_status='invalid',
            performance_status='fail', cleanup_status='incomplete'))
    return LifecycleRecord(**dict(data, **changes))


def event(kind, revision=0, **changes):
    return LifecycleEvent(**dict(dict(schema_version=1, event_id=f'event/{revision}/{kind}',
        kind=kind, source='service/a', expected_revision=revision,
        evidence_refs=('proof/a',), observed_at=10.0), **changes))


def guards(**changes):
    return TransitionGuards(**dict(dict(schema_version=1, work_lease_valid=True,
        attempt_closed=True, disposition_closed=True, effect_known=True,
        action_registered=True, authority_valid=True, budget_available=True,
        safety_budget_available=True, safety_deadline=110.0, observation_budget_available=True,
        postcheck_verified=True, recovery_plan_valid=True, node_acceptance_valid=True,
        promotion_proof_valid=True, worker_ready=True, successor_bound=True,
        decision_valid=True, dependency_valid=True, proof_valid=True,
        resume_authorized=True, partial_close_authorized=True,
        effects_in_flight=False, same_failure_count=0, max_same_failure=2,
        new_evidence=True), **changes))


# Literal expectations from ALD-02a; changing the production table cannot change this oracle.
EDGES = [
    ('draining','disposition_confirmed','diagnosing'),
    ('draining','effect_unknown','reconciling'),
    ('reconciling','effect_known','diagnosing'),
    ('reconciling','observation_due','reconciling'),
    ('reconciling','budget_exhausted','blocked'),
    ('diagnosing','known_action','remediation_pending'),
    ('diagnosing','needs_code','candidate_building'),
    ('diagnosing','needs_input','waiting_decision'),
    ('diagnosing','no_safe_route','blocked'),
    ('remediation_pending','claimed','remediating'),
    ('remediating','postcheck_pass','reentering'),
    ('remediating','postcheck_fail_with_budget','diagnosing'),
    ('remediating','postcheck_fail_with_budget','backoff'),
    ('remediating','postcheck_fail_exhausted','blocked'),
    ('remediating','unknown','reconciling'),
    ('backoff','timer_due','diagnosing'),
    ('reentering','node_accepted','fulfilled'),
    ('reentering','node_failed','draining'),
    ('candidate_building','revision_frozen','candidate_verifying'),
    ('candidate_building','provider_unknown','reconciling'),
    ('candidate_building','budget_exhausted','blocked'),
    ('candidate_verifying','tests_fail','candidate_building'),
    ('candidate_verifying','tests_pass','promotion_pending'),
    ('candidate_verifying','environment_missing','waiting_dependency'),
    ('promotion_pending','approved_and_ready','superseded'),
    ('promotion_pending','needs_authorization','waiting_decision'),
    ('promotion_pending','worker_missing','verified_waiting_worker'),
    ('promotion_pending','proof_invalid','candidate_verifying'),
    ('promotion_pending','nonruntime_candidate_accepted','diagnosing'),
    ('promotion_pending','nonruntime_candidate_accepted','blocked'),
    ('verified_waiting_worker','ready','promotion_pending'),
    ('verified_waiting_worker','proof_expired','candidate_verifying'),
    ('waiting_decision','valid_decision','diagnosing'),
    ('waiting_dependency','dependency_ready','candidate_verifying'),
    ('cancelling','disposition_closed','cancelled'),
    ('cancelling','unknown','blocked'),
    ('blocked','authorized_resume','diagnosing'),
    ('blocked','close_partial','blocked_closed'),
]


@pytest.mark.parametrize('phase,kind,want', EDGES)
def test_declared_edges(phase, kind, want):
    changes = {}
    record_changes = dict(resume_phase='candidate_verifying' if phase == 'waiting_dependency' else 'diagnosing')
    if want == 'reconciling' or kind == 'unknown':
        changes.update(operation_kind='provider' if kind == 'provider_unknown' else 'site',
                       operation_refs=('op/a',), isolation_refs=('isolation/a',))
    if want == 'backoff':
        changes['next_due_at'] = 20.0
    if phase == 'backoff':
        record_changes['next_due_at'] = 10.0
    if phase == 'cancelling':
        record_changes.update(stop_reason='explicit_cancel', terminal_intent='cancelled')
    if kind == 'nonruntime_candidate_accepted':
        changes['resume_phase'] = want
    original = record(phase, **record_changes)
    result = transition_lifecycle(original, event(kind, **changes), guards())
    assert result.phase == want
    assert result.revision == 1
    assert result.node_result == original.node_result
    assert original.revision == 0


NONTERMINAL_PHASES = ['draining', 'reconciling', 'diagnosing', 'remediation_pending',
    'remediating', 'backoff', 'reentering', 'candidate_building', 'candidate_verifying',
    'promotion_pending', 'verified_waiting_worker', 'waiting_decision', 'waiting_dependency',
    'cancelling', 'blocked']
LOCAL_EVENTS = sorted({kind for _, kind, _ in EDGES} - {'budget_exhausted'})


@pytest.mark.parametrize('phase,kind', [(phase, kind)
    for phase in NONTERMINAL_PHASES for kind in LOCAL_EVENTS
    if not any(source == phase and evt == kind for source, evt, _ in EDGES)])
def test_unlisted_edges_are_rejected(phase, kind):
    with pytest.raises(InvalidLifecycleTransition):
        transition_lifecycle(record(phase), event(kind), guards())


@pytest.mark.parametrize('phase', ['fulfilled', 'superseded', 'blocked_closed', 'cancelled', 'failed'])
@pytest.mark.parametrize('kind', ['cancel', 'owner_lost', 'deadline', 'known_action', 'authorized_resume'])
def test_terminal_phases_do_not_dispatch_or_resume(phase, kind):
    with pytest.raises(InvalidLifecycleTransition):
        transition_lifecycle(record(phase), event(kind), guards())


def test_failure_with_new_strategy_loops():
    current = record()
    phases = []
    for kind in ['known_action', 'claimed', 'postcheck_fail_with_budget',
                 'known_action', 'claimed', 'postcheck_pass']:
        current = transition_lifecycle(current, event(kind, current.revision), guards())
        phases.append(current.phase)
    assert phases == ['remediation_pending', 'remediating', 'diagnosing',
                      'remediation_pending', 'remediating', 'reentering']


def test_same_failure_without_new_evidence_cannot_loop():
    with pytest.raises(InvalidLifecycleTransition):
        transition_lifecycle(record('remediating'), event('postcheck_fail_with_budget'),
            guards(same_failure_count=2, new_evidence=False))


def test_last_allowed_attempt_can_succeed():
    current = transition_lifecycle(record('remediating'), event('postcheck_pass'),
                                   guards(budget_available=False))
    assert current.phase == 'reentering'
    assert transition_lifecycle(current, event('node_accepted', 1),
                                guards(budget_available=False)).phase == 'fulfilled'
    with pytest.raises(InvalidLifecycleTransition):
        transition_lifecycle(record('diagnosing'), event('known_action'), guards(budget_available=False))
    assert transition_lifecycle(record('candidate_verifying'), event('tests_pass'),
                                guards(budget_available=False)).phase == 'promotion_pending'


@pytest.mark.parametrize('reason,want', [('cancel','cancelled'), ('deadline','blocked'), ('budget_exhausted','blocked')])
def test_stop_reason_survives_disposition(reason, want):
    current = transition_lifecycle(record('remediating', operation_kind='site', operation_refs=('op/a',)),
        event(reason), guards(effects_in_flight=True))
    assert current.phase == 'cancelling'
    assert current.terminal_intent == want
    current = transition_lifecycle(current, event('disposition_closed', 1), guards())
    assert current.phase == want
    assert current.stop_reason == ('explicit_cancel' if reason == 'cancel' else reason)


def test_explicit_cancel_takes_priority_over_deadline():
    current = transition_lifecycle(record('remediating', operation_kind='site', operation_refs=('op/a',)),
        event('deadline'), guards(effects_in_flight=True))
    current = transition_lifecycle(current, event('cancel', 1), guards())
    current = transition_lifecycle(current, event('deadline', 2), guards(effects_in_flight=True))
    assert current.terminal_intent == 'cancelled'
    assert current.stop_reason == 'explicit_cancel'


@pytest.mark.parametrize('phase', ['remediation_pending','backoff','verified_waiting_worker','waiting_dependency','waiting_decision'])
def test_waiting_deadline_blocks_and_preserves_node_status(phase):
    original = record(phase)
    result = transition_lifecycle(original, event('deadline', observed_at=101.0), guards())
    assert result.phase == 'blocked'
    assert result.node_result.performance_status == 'fail'


def test_unknown_effects_keep_quarantine_after_safety_exhaustion():
    current = transition_lifecycle(record('remediating'),
        event('budget_exhausted', operation_kind='site', operation_refs=('op/a',),
              isolation_refs=('isolation/a',)),
        guards(effects_in_flight=True, effect_known=False, disposition_closed=False,
               safety_budget_available=False))
    assert current.phase == 'blocked'
    assert current.isolation_refs == ('isolation/a',)
    with pytest.raises(InvalidLifecycleTransition):
        transition_lifecycle(current, event('authorized_resume', 1), guards(effect_known=False))
    result = transition_lifecycle(current, event('close_partial', 1), guards())
    assert result.phase == 'blocked_closed'
    assert result.isolation_refs == ('isolation/a',)


def test_receipt_idempotency_and_stale_event_rejection():
    evt = event('known_action')
    result = transition_lifecycle(record(), evt, guards())
    assert transition_lifecycle(result, evt, guards()) == result
    with pytest.raises(StaleLifecycleEvent):
        transition_lifecycle(result, event('claimed'), guards())
    with pytest.raises(InvalidLifecycleTransition):
        transition_lifecycle(result, event('claimed', 1, event_id=evt.event_id), guards())


@pytest.mark.parametrize('phase,kind', [('diagnosing','known_action'), ('diagnosing','needs_code'),
    ('remediation_pending','claimed'), ('candidate_verifying','tests_fail'),
    ('promotion_pending','approved_and_ready')])
def test_expired_deadline_cannot_authorize_new_work(phase, kind):
    with pytest.raises(InvalidLifecycleTransition):
        transition_lifecycle(record(phase), event(kind, observed_at=100.0), guards())


@pytest.mark.parametrize('phase,kind', [('draining','disposition_confirmed'),
    ('diagnosing','known_action'), ('remediation_pending','claimed'),
    ('remediating','postcheck_pass'), ('reentering','node_accepted'),
    ('promotion_pending','approved_and_ready'), ('blocked','authorized_resume')])
def test_defaults_do_not_authorize_transitions(phase, kind):
    with pytest.raises(InvalidLifecycleTransition):
        transition_lifecycle(record(phase), event(kind), TransitionGuards(schema_version=1))


def test_closure_waits_for_disposition_and_successor():
    for kwargs in [dict(disposition_closed=False), dict(successor_bound=False), dict(worker_ready=False)]:
        with pytest.raises(InvalidLifecycleTransition):
            transition_lifecycle(record('promotion_pending'), event('approved_and_ready'), guards(**kwargs))
    with pytest.raises(InvalidLifecycleTransition):
        transition_lifecycle(record('diagnosing'), event('irrecoverable_contract_error'),
                             guards(disposition_closed=False))
    assert transition_lifecycle(record('diagnosing'), event('irrecoverable_contract_error'),
                                guards()).phase == 'failed'


def test_reconciliation_cannot_resume_arbitrary_phase_or_dispatch_unknown_operation():
    with pytest.raises(InvalidLifecycleTransition):
        transition_lifecycle(record('reconciling', resume_phase='fulfilled'), event('effect_known'), guards())
    with pytest.raises(InvalidLifecycleTransition):
        transition_lifecycle(record('remediating'), event('unknown'), guards())
    current = transition_lifecycle(record('candidate_building'),
        event('provider_unknown', operation_kind='provider', operation_refs=('provider/op',),
              isolation_refs=('candidate/isolation',)), guards())
    assert current.resume_phase == 'candidate_building'
    assert transition_lifecycle(current, event('effect_known', 1), guards()).phase == 'candidate_building'


def test_backoff_requires_due_time_and_observation_requires_safety_allowance():
    with pytest.raises(InvalidLifecycleTransition):
        transition_lifecycle(record('backoff', next_due_at=20.0), event('timer_due'), guards())
    with pytest.raises(InvalidLifecycleTransition):
        transition_lifecycle(record('reconciling'), event('observation_due'),
                             guards(observation_budget_available=False))


def test_owner_loss_does_not_create_attempt_or_change_phase():
    original = record('remediating', attempt_refs=('attempt/a',))
    result = transition_lifecycle(original, event('owner_lost'), guards())
    assert result.phase == 'remediating'
    assert result.attempt_refs == ('attempt/a',)


def test_blocked_route_retains_authorized_recovery_entrance():
    current = transition_lifecycle(record(), event('no_safe_route'), guards())
    assert current.resume_phase == 'diagnosing'
    result = transition_lifecycle(current, event('authorized_resume', 1), guards())
    assert result.phase == 'diagnosing'
    assert result.terminal_intent is None


@pytest.mark.parametrize('phase,kind', [('candidate_building','revision_frozen'),
    ('candidate_verifying','tests_pass'), ('candidate_verifying','tests_fail')])
def test_candidate_facts_require_verified_evidence(phase, kind):
    with pytest.raises(InvalidLifecycleTransition):
        transition_lifecycle(record(phase), event(kind), guards(proof_valid=False))


@pytest.mark.parametrize('phase,kind', [('diagnosing','needs_input'),
    ('candidate_verifying','environment_missing'), ('promotion_pending','needs_authorization'),
    ('remediating','unknown')])
def test_event_cannot_choose_another_resume_destination(phase, kind):
    with pytest.raises(InvalidLifecycleTransition):
        transition_lifecycle(record(phase), event(kind, resume_phase='reentering',
            operation_kind='site', operation_refs=('op/a',), isolation_refs=('isolation/a',)), guards())


def test_cancel_with_unknown_effect_requires_and_retains_quarantine():
    with pytest.raises(InvalidLifecycleTransition):
        transition_lifecycle(record('remediating'), event('cancel'),
            guards(effect_known=False, effects_in_flight=True, disposition_closed=False))
    result = transition_lifecycle(record('remediating'),
        event('cancel', isolation_refs=('isolation/a',), operation_kind='site', operation_refs=('op/a',)),
        guards(effect_known=False, effects_in_flight=True, disposition_closed=False))
    assert result.phase == 'cancelling'
    assert result.isolation_refs == ('isolation/a',)


def test_observation_cannot_continue_past_business_deadline_without_stop_intent():
    with pytest.raises(InvalidLifecycleTransition):
        transition_lifecycle(record('reconciling'), event('observation_due', observed_at=101.0), guards())


def test_safety_observation_is_bounded_by_its_own_absolute_deadline():
    with pytest.raises(InvalidLifecycleTransition):
        transition_lifecycle(record('reconciling', stop_reason='budget_exhausted',
            terminal_intent='blocked'), event('observation_due', observed_at=120.0),
            guards(safety_deadline=110.0))


@pytest.mark.parametrize('phase,kind,missing', [
    ('draining','disposition_confirmed','attempt_closed'),
    ('draining','disposition_confirmed','disposition_closed'),
    ('diagnosing','known_action','action_registered'),
    ('diagnosing','known_action','authority_valid'),
    ('remediation_pending','claimed','work_lease_valid'),
    ('remediating','postcheck_pass','postcheck_verified'),
    ('remediating','postcheck_pass','effect_known'),
    ('reentering','node_accepted','recovery_plan_valid'),
    ('reentering','node_accepted','node_acceptance_valid'),
    ('promotion_pending','approved_and_ready','promotion_proof_valid'),
    ('promotion_pending','approved_and_ready','authority_valid'),
    ('waiting_decision','valid_decision','decision_valid'),
    ('waiting_dependency','dependency_ready','dependency_valid'),
    ('blocked','authorized_resume','resume_authorized'),
    ('blocked','close_partial','partial_close_authorized'),
])
def test_individual_required_facts_cannot_be_omitted(phase, kind, missing):
    with pytest.raises(InvalidLifecycleTransition):
        transition_lifecycle(record(phase, resume_phase='diagnosing'), event(kind), guards(**{missing: False}))


@pytest.mark.parametrize('phase', NONTERMINAL_PHASES)
@pytest.mark.parametrize('kind,want', [('cancel','cancelling'), ('deadline','blocked'),
    ('budget_exhausted','blocked'), ('owner_lost',None), ('irrecoverable_contract_error','failed')])
def test_global_nonterminal_events(phase, kind, want):
    original = record(phase, resume_phase='diagnosing')
    result = transition_lifecycle(original, event(kind, observed_at=101.0), guards())
    assert result.phase == (phase if want is None else want)
    assert result.node_result == original.node_result


def test_safety_expiry_prevents_further_inflight_disposition():
    result = transition_lifecycle(record('remediating', operation_kind='site', operation_refs=('op/a',)),
        event('deadline', observed_at=120.0, isolation_refs=('isolation/a',)),
        guards(effects_in_flight=True, safety_deadline=110.0))
    assert result.phase == 'blocked'
    assert result.isolation_refs == ('isolation/a',)


def test_expired_proof_returns_to_verification_without_budget_refresh():
    original = record('verified_waiting_worker', attempt_refs=('attempt/a',))
    result = transition_lifecycle(original, event('proof_expired'), guards(budget_available=False))
    assert result.phase == 'candidate_verifying'
    assert result.attempt_refs == ('attempt/a',)


def test_safety_unknown_preserves_recoverable_phase_through_authorized_resume():
    current = transition_lifecycle(record('remediating', operation_kind='site', operation_refs=('op/a',)),
        event('budget_exhausted'),
        guards(effects_in_flight=True, disposition_closed=False))
    assert current.phase == 'cancelling'
    assert current.resume_phase == 'remediating'
    current = transition_lifecycle(current,
        event('unknown', 1, operation_kind='site', operation_refs=('op/a',),
              isolation_refs=('isolation/a',)),
        guards(effect_known=False, effects_in_flight=True, disposition_closed=False))
    assert current.phase == 'blocked'
    assert current.resume_phase == 'remediating'
    current = transition_lifecycle(current, event('authorized_resume', 2), guards())
    assert current.phase == 'remediating'
    assert current.stop_reason is None
    assert current.terminal_intent is None
    current = transition_lifecycle(current, event('postcheck_pass', 3), guards())
    current = transition_lifecycle(current, event('node_accepted', 4), guards())
    assert current.phase == 'fulfilled'


@pytest.mark.parametrize('kind', ['budget_exhausted', 'deadline', 'cancel'])
@pytest.mark.parametrize('binding', [{}, dict(operation_kind='site'),
    dict(operation_refs=('op/a',))])
def test_stopping_inflight_effect_requires_complete_operation_binding(kind, binding):
    with pytest.raises(InvalidLifecycleTransition):
        transition_lifecycle(record('remediating'),
            event(kind, isolation_refs=('isolation/a',), **binding),
            guards(effects_in_flight=True, effect_known=False, disposition_closed=False))


@pytest.mark.parametrize('kind', ['budget_exhausted', 'deadline', 'cancel'])
def test_stopping_known_inflight_effect_still_needs_original_operation(kind):
    with pytest.raises(InvalidLifecycleTransition):
        transition_lifecycle(record('remediating'), event(kind),
            guards(effects_in_flight=True, disposition_closed=False))


@pytest.mark.parametrize('binding', [dict(operation_kind='site', operation_refs=('site/different',)),
    dict(operation_kind='provider', operation_refs=('provider/different',)),
    dict(operation_kind='site', operation_refs=('provider/original',)),
    dict(operation_kind='provider'), dict(operation_refs=('provider/original',))])
def test_stop_event_cannot_swap_or_partially_replace_bound_provider_operation(binding):
    current = transition_lifecycle(record('candidate_building'),
        event('provider_unknown', operation_kind='provider', operation_refs=('provider/original',),
            isolation_refs=('candidate/isolation',)), guards())
    with pytest.raises(InvalidLifecycleTransition):
        transition_lifecycle(current, event('deadline', 1, observed_at=101.0, **binding),
            guards(effects_in_flight=True, effect_known=False, disposition_closed=False))


@pytest.mark.parametrize('repeat_binding', [False, True])
def test_stop_event_preserves_bound_provider_operation(repeat_binding):
    current = transition_lifecycle(record('candidate_building'),
        event('provider_unknown', operation_kind='provider', operation_refs=('provider/original',),
            isolation_refs=('candidate/isolation',)), guards())
    binding = dict(operation_kind='provider', operation_refs=('provider/original',)) if repeat_binding else {}
    result = transition_lifecycle(current, event('deadline', 1, observed_at=101.0, **binding),
        guards(effects_in_flight=True, effect_known=False, disposition_closed=False))
    assert result.phase == 'reconciling'
    assert result.operation_kind == 'provider'
    assert result.operation_refs == ('provider/original',)
    assert result.resume_phase == 'candidate_building'


@pytest.mark.parametrize('binding', [dict(operation_kind='provider', operation_refs=('provider/new',)),
    dict(operation_kind='site', operation_refs=('site/new',))])
def test_unknown_safety_result_cannot_replace_original_operation(binding):
    current = transition_lifecycle(record('remediating', operation_kind='site', operation_refs=('op/a',)),
        event('budget_exhausted'), guards(effects_in_flight=True, disposition_closed=False))
    with pytest.raises(InvalidLifecycleTransition):
        transition_lifecycle(current, event('unknown', 1, isolation_refs=('isolation/a',), **binding),
            guards(effects_in_flight=True, effect_known=False, disposition_closed=False))
