"""ALU-01: boundary contracts reject ambiguous budgets and capability grants."""
import pytest
from pydantic import ValidationError

from sandbox_graph.domain.agent_lifecycle import (
    BackoffPolicy, BudgetUsage, ContextPacket, LifecycleKey, LifecycleRecord,
    RepairOutcome, RepairPolicy, RepairRequest, ResourceDispositionReceipt,
    RecoveryPlan, SafetyBudget,
)


def key(**changes):
    return LifecycleKey(**dict(dict(schema_version=1, run_ref='run/a', scope_kind='work',
        scope_ref='work/a', node_id='N02', lifecycle_id='life/a',
        lineage_budget_ref='lineage/a'), **changes))


def policy_data():
    return dict(schema_version=1, policy_version='v1', actions=(),
        max_repair_attempts=2, max_diagnosis_calls=2, max_candidate_revisions=2,
        token_limit=30, token_unit='tokens', child_job_limit=2, max_depth=2,
        max_concurrency=1, deadline=100.0, clock='wall',
        backoff=dict(schema_version=1, seconds=2.0, unit='seconds'),
        max_same_failure=2, max_observations=2,
        safety_budget=dict(schema_version=1, max_stop_calls=2, max_query_calls=2,
            max_cleanup_calls=2, cost_limit=1.0, cost_unit='USD', deadline=110.0,
            clock='wall'), postcheck_ref='check/v1')


def test_scope_and_policy_validation():
    assert key().scope_ref == 'work/a'
    assert key(node_id='N09', lifecycle_id='life/b') != key()
    value = RepairPolicy(**policy_data())
    assert value.max_repair_attempts == 2
    assert value.actions == ()  # No implicit registered action or tool capability.
    assert value.model_dump()['safety_budget']['cost_unit'] == 'USD'


@pytest.mark.parametrize('field', ['max_repair_attempts', 'max_diagnosis_calls',
    'max_candidate_revisions', 'token_limit', 'child_job_limit', 'max_depth',
    'max_concurrency', 'max_same_failure', 'max_observations'])
@pytest.mark.parametrize('invalid', [0, -1, True, 1.5, '2'])
def test_policy_requires_positive_integer_limits(field, invalid):
    data = policy_data()
    data[field] = invalid
    with pytest.raises(ValidationError):
        RepairPolicy(**data)


@pytest.mark.parametrize('field', ['schema_version', 'policy_version', 'deadline',
    'backoff', 'max_same_failure', 'max_observations', 'safety_budget', 'postcheck_ref'])
def test_policy_has_no_unbounded_missing_defaults(field):
    data = policy_data()
    del data[field]
    with pytest.raises(ValidationError):
        RepairPolicy(**data)


@pytest.mark.parametrize('invalid', [float('nan'), float('inf'), -float('inf'), -1.0, True])
def test_limits_and_times_are_finite_and_unit_bound(invalid):
    data = policy_data()
    data['deadline'] = invalid
    with pytest.raises(ValidationError):
        RepairPolicy(**data)
    with pytest.raises(ValidationError):
        BackoffPolicy(schema_version=1, seconds=invalid, unit='seconds')
    data = policy_data()['safety_budget']
    data['cost_limit'] = invalid
    with pytest.raises(ValidationError):
        SafetyBudget(**data)


@pytest.mark.parametrize('changes', [dict(schema_version=True), dict(schema_version=2),
    dict(scope_kind='anything'), dict(node_id=''), dict(run_ref=' '),
    dict(scope_ref='work/a work/b'), dict(extra='ignored')])
def test_identity_and_version_are_strict(changes):
    with pytest.raises(ValidationError):
        key(**changes)


def test_request_cannot_self_assign_service_owned_fields():
    data = dict(schema_version=1, request_id='request/a', lifecycle_id='life/a',
        source_receipt='receipt/a', expected_revision=0, action_ref='repair/v1',
        input_manifest_ref='input/a', proposal_ref='proposal/a', authorization_ref='auth/a')
    assert RepairRequest(**data).request_id == 'request/a'
    for field in ['attempt', 'owner', 'dispatch_generation', 'remaining_budget', 'resources_disposed']:
        with pytest.raises(ValidationError):
            RepairRequest(**data, **{field: 1})


def test_disposition_requires_structured_timed_proof():
    data = dict(schema_version=1, work_ref='work/a', operation_refs=('op/a',),
        stopped=True, admission_status='closed', sampler_status='stopped',
        owned_resources=('resource/a',), residual_resources=(), isolation_domains=(),
        observed_at=10.0, expires_at=20.0, clock='wall', producer='runner/a',
        producer_version='v1')
    assert ResourceDispositionReceipt(**data).expires_at == 20.0
    for changes in [dict(expires_at=9.0), dict(stopped='true'),
                    dict(resources_disposed=True), dict(producer='')]:
        with pytest.raises(ValidationError):
            ResourceDispositionReceipt(**dict(data, **changes))


def test_context_does_not_accept_conversation_or_implicit_tools():
    packet = dict(schema_version=1, key=key(), node_supervisor_id='supervisor/N02',
        task_role='diagnosis', graph_ref='graph/v1', plan_ref='plan/v1',
        contract_ref='contract/v1', failure_summary='Fixture creation failed',
        facts=('receipt reports missing fixture',), hypotheses=(),
        evidence_refs=('receipt/a',), evidence_scope_refs=('work/a',),
        evidence_byte_limit=1024, budget_usage=BudgetUsage(schema_version=1),
        disposition_receipt_ref='disposition/a')
    assert ContextPacket(**packet).tool_capabilities == ()
    for field in ['parent_conversation', 'other_group_logs', 'plaintext_secrets']:
        with pytest.raises(ValidationError):
            ContextPacket(**packet, **{field: 'canary'})


def test_recovery_and_outcome_keep_independent_statuses():
    plan = RecoveryPlan(schema_version=1, ref='recovery/a', graph_ref='graph/v1',
        graph_hash='a'*64, contract_ref='contract/v1', contract_hash='b'*64,
        input_hash='c'*64, work_revision=2, source_attempt='attempt/1', entry_node='N01',
        revalidate_nodes=('N01', 'N02'), reuse_receipts=(), invalidated_refs=('fixture/a',),
        resource_proofs=('disposition/a',), measurement_generation=1)
    assert plan.entry_node == 'N01'
    with pytest.raises(ValidationError):
        RecoveryPlan(**dict(plan.model_dump(), resume_from='N05'))
    outcome = RepairOutcome(schema_version=1, order_ref='order/a', operation_refs=('op/a',),
        effect_certainty='unknown', execution_status='succeeded', data_status='unverified',
        cleanup_status='unknown', postcheck_ref='postcheck/a', raw_refs=('raw/a',))
    assert outcome.effect_certainty == 'unknown'
    with pytest.raises(ValidationError):
        RepairOutcome(**dict(outcome.model_dump(), repair_succeeded=True))


def test_record_requires_identity_and_does_not_accept_flat_result_overwrites():
    record = LifecycleRecord(schema_version=1, key=key(), graph_ref='graph/v1',
        plan_ref='plan/v1', contract_ref='contract/v1', policy_ref='policy/v1',
        phase='draining', revision=0, owner_epoch=1, source_receipt='receipt/a',
        issue_fingerprint='a'*64, deadline=100.0)
    for field in ['execution_status', 'performance_status', 'cleanup_status', 'data_status']:
        with pytest.raises(ValidationError):
            LifecycleRecord(**dict(record.model_dump(), **{field: 'pass'}))


def test_required_versions_and_units_survive_json_roundtrip():
    policy = RepairPolicy(**policy_data())
    assert RepairPolicy.model_validate_json(policy.model_dump_json()) == policy
    for field, value in [('token_unit', 'bytes'), ('clock', 'monotonic')]:
        with pytest.raises(ValidationError):
            RepairPolicy(**dict(policy_data(), **{field: value}))
    data = policy_data()
    data['backoff']['unbounded'] = True
    with pytest.raises(ValidationError):
        RepairPolicy(**data)


def test_safety_budget_has_a_separate_finite_horizon():
    data = policy_data()
    data['safety_budget']['deadline'] = 90.0
    with pytest.raises(ValidationError):
        RepairPolicy(**data)
    for field in ['max_stop_calls', 'max_query_calls', 'max_cleanup_calls']:
        data = policy_data()
        data['safety_budget'][field] = True
        with pytest.raises(ValidationError):
            RepairPolicy(**data)


@pytest.mark.parametrize('changes', [dict(stop_reason='explicit_cancel'),
    dict(terminal_intent='blocked'), dict(stop_reason='explicit_cancel', terminal_intent='blocked'),
    dict(stop_reason='deadline', terminal_intent='cancelled')])
def test_stop_reason_and_terminal_intent_are_an_atomic_pair(changes):
    data = dict(schema_version=1, key=key(), graph_ref='graph/v1', plan_ref='plan/v1',
        contract_ref='contract/v1', policy_ref='policy/v1', phase='cancelling', revision=0,
        owner_epoch=1, source_receipt='receipt/a', issue_fingerprint='a'*64, deadline=100.0)
    with pytest.raises(ValidationError):
        LifecycleRecord(**dict(data, **changes))
