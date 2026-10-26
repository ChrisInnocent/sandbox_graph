"""ALU-14/16/18: content identity, frozen targets and evidence contracts."""
import pytest
from pydantic import ValidationError

from sandbox_graph.domain.evolution import (
    CandidateRevision, ChangeProposal, GateCaseResult, GateManifest, GateResult,
    PromotionRecord, ReviewFinding, ReviewRecord,
)


def target():
    return dict(target_plan_ref='plan/2', target_plan_hash='1'*64,
        target_contract_ref='contract/2', target_contract_hash='2'*64,
        target_auth_ref='auth/run-2', target_auth_hash='3'*64, target_auth_revision=2)


def candidate_data(**changes):
    return dict(dict(schema_version=1, candidate_ref='candidate/1', proposal_id='proposal/1',
        revision=1, parent_revision=None, base_commit='a'*40, tree_hash='a'*64,
        patch_hash='b'*64, graph_ref='graph/2', graph_hash='c'*64, lock_hash='d'*64,
        changed_paths=('src/adapter.py', 'tests/test_adapter.py'), risk='R2',
        workspace_ref='workspace/1', budget_ref='budget/lineage-1', **target()), **changes)


def proposal_data():
    return dict(schema_version=1, proposal_id='proposal/1', source_key=dict(
        schema_version=1, run_ref='run/1', scope_kind='work', scope_ref='work/1',
        node_id='N02', lifecycle_id='lifecycle/1', lineage_budget_ref='budget/lineage-1'),
        base_code_ref='code/1', base_graph_ref='graph/1', failure_evidence_refs=('receipt/fail',),
        facts=('fixture is absent',), hypotheses=('adapter omitted fixture',),
        target_behavior='Create the contracted fixture', allowed_paths=('src/adapter.py',),
        node_scope=('N02',), contract_scope=('contract/2',), acceptance_refs=('case/fixture',),
        scope_auth='decision/scope-1')


def gate_data():
    candidate = CandidateRevision(**candidate_data())
    return dict(schema_version=1, manifest_ref='gate/1', candidate_ref=candidate.candidate_ref,
        candidate_hash=candidate.candidate_hash, **target(), trusted_suite_hash='4'*64,
        policy_hash='5'*64, environment_ref='environment/unit', environment_hash='6'*64,
        runner_ref='runner/1', runner_version='1', runner_bundle_hash='7'*64,
        required_gates=('G0', 'G1'), expected_case_ids=('case/fixture',),
        actual_case_ids=('case/fixture',), results=(
            GateResult(schema_version=1, gate_id='G0', status='pass', case_ids=(), log_hash='8'*64),
            GateResult(schema_version=1, gate_id='G1', status='pass',
                case_ids=('case/fixture',), log_hash='8'*64)),
        case_results=(GateCaseResult(schema_version=1, case_id='case/fixture', status='pass',
            log_hash='8'*64),), log_hash='9'*64, producer_identity='trusted-runner/1',
        producer_version='1', observed_at=10.0, expires_at=20.0, clock='wall')


def review_data():
    return dict(schema_version=1, review_ref='review/1', candidate_ref='candidate/1',
        candidate_hash=CandidateRevision(**candidate_data()).candidate_hash, **target(),
        gate_manifest_ref='gate/1', gate_manifest_hash='a'*64, policy_hash='5'*64,
        coder_identity='coder/1', reviewer_identity='reviewer/1', reviewer_kind='model',
        producer_version='model/1', result='approved', findings=(), evidence_refs=('diff/1',),
        observed_at=11.0, expires_at=20.0, clock='wall')


def promotion_data():
    return dict(schema_version=1, request_id='promotion/request-1', candidate_ref='candidate/1',
        candidate_hash=CandidateRevision(**candidate_data()).candidate_hash, **target(),
        gate_manifest_ref='gate/1', gate_manifest_hash='a'*64, review_ref='review/1',
        review_hash='b'*64, authorization_ref='decision/publish-1', authorization_hash='c'*64,
        authorization_revision=1, worker_bundle_ref='bundle/1', worker_bundle_hash='d'*64,
        expected_predecessor_revision=0, created_at=12.0, expires_at=20.0, clock='wall')


def test_candidate_identity_excludes_record_provenance_and_revision_number():
    original = CandidateRevision(**candidate_data())
    copied = CandidateRevision(**candidate_data(candidate_ref='candidate/2', proposal_id='proposal/2',
        revision=2, parent_revision=1, workspace_ref='workspace/2', budget_ref='budget/other'))
    assert copied.candidate_hash == original.candidate_hash
    assert len(original.candidate_hash) == 64
    assert original.content_identity()['target_auth_hash'] == '3'*64
    assert 'workspace_ref' not in original.content_identity()


@pytest.mark.parametrize('field,value', [
    ('base_commit', 'f'*40), ('tree_hash', 'f'*64), ('patch_hash', 'f'*64),
    ('graph_ref', 'graph/3'), ('graph_hash', 'f'*64), ('lock_hash', 'f'*64),
    ('changed_paths', ('src/other.py',)), ('risk', 'R3'),
    ('target_plan_ref', 'plan/3'), ('target_plan_hash', 'f'*64),
    ('target_contract_ref', 'contract/3'), ('target_contract_hash', 'f'*64),
    ('target_auth_ref', 'auth/run-3'), ('target_auth_hash', 'f'*64), ('target_auth_revision', 3),
])
def test_content_or_frozen_target_changes_invalidate_old_gate(field, value):
    original = CandidateRevision(**candidate_data())
    changed = CandidateRevision(**candidate_data(**{field: value}))
    assert changed.candidate_hash != original.candidate_hash
    assert not GateManifest(**gate_data()).matches_candidate(changed)
    assert original.tree_hash == 'a'*64  # Old revision remains available unchanged.


def test_path_set_order_does_not_create_different_content_identity():
    a = CandidateRevision(**candidate_data())
    b = CandidateRevision(**candidate_data(changed_paths=tuple(reversed(a.changed_paths))))
    assert a.candidate_hash == b.candidate_hash


@pytest.mark.parametrize('path', ['/tmp/a', '../a', 'src/../a', './a', 'a//b', 'a\\b', 'a\x00b', 'a/'])
def test_candidate_rejects_noncanonical_or_escaping_paths(path):
    with pytest.raises(ValidationError):
        CandidateRevision(**candidate_data(changed_paths=(path,)))


@pytest.mark.parametrize('changes', [dict(revision=True), dict(revision=0),
    dict(revision=2, parent_revision=None), dict(parent_revision=1),
    dict(target_auth_revision=True), dict(target_auth_revision=-1), dict(tree_hash='abc'),
    dict(risk='low'), dict(changed_paths=('a', 'a')), dict(changed_paths=()),
    dict(changed_paths=tuple(f'src/{i}' for i in range(1025)))])
def test_candidate_rejects_ambiguous_revision_identity_and_unbounded_paths(changes):
    with pytest.raises(ValidationError):
        CandidateRevision(**candidate_data(**changes))


@pytest.mark.parametrize('factory,data', [
    (ChangeProposal, proposal_data), (CandidateRevision, candidate_data),
    (GateManifest, gate_data), (ReviewRecord, review_data), (PromotionRecord, promotion_data),
])
def test_contracts_are_strict_frozen_and_roundtrip_without_implicit_authority(factory, data):
    value = factory(**data())
    assert factory.model_validate_json(value.model_dump_json()) == value
    with pytest.raises(ValidationError):
        value.schema_version = 2
    for field in ('authorization_valid', 'trusted', 'resources_disposed', 'remaining_budget'):
        with pytest.raises(ValidationError):
            factory(**dict(data(), **{field: True}))
    for version in (True, '1', 2):
        with pytest.raises(ValidationError):
            factory(**dict(data(), schema_version=version))


@pytest.mark.parametrize('field', list(target()))
def test_frozen_target_has_no_implicit_latest_or_inherited_defaults(field):
    for factory, data in ((CandidateRevision, candidate_data), (GateManifest, gate_data),
                          (ReviewRecord, review_data), (PromotionRecord, promotion_data)):
        inputs = data()
        del inputs[field]
        with pytest.raises(ValidationError):
            factory(**inputs)


@pytest.mark.parametrize('changes', [dict(failure_evidence_refs=()), dict(facts=()),
    dict(allowed_paths=('../other',)), dict(target_behavior=' '), dict(scope_auth=''),
    dict(acceptance_refs=()), dict(facts=('x'*16385,))])
def test_proposal_requires_bounded_evidence_and_explicit_scope(changes):
    with pytest.raises(ValidationError):
        ChangeProposal(**dict(proposal_data(), **changes))


def test_gate_input_identity_excludes_results_and_producer_but_integrity_hash_does_not():
    original = GateManifest(**gate_data())
    changed = GateManifest(**dict(gate_data(), producer_identity='unverified-model/1',
        log_hash='f'*64, observed_at=11.0))
    assert changed.input_hash == original.input_hash
    assert changed.manifest_hash != original.manifest_hash
    # An identity string is provenance, never evidence of a trusted producer.
    assert changed.matches_candidate(CandidateRevision(**candidate_data()))


@pytest.mark.parametrize('field', ['trusted_suite_hash', 'policy_hash', 'environment_hash',
    'runner_bundle_hash', 'target_auth_hash'])
def test_gate_input_digest_changes_with_trusted_execution_inputs(field):
    original = GateManifest(**gate_data())
    changed = GateManifest(**dict(gate_data(), **{field: 'f'*64}))
    assert changed.input_hash != original.input_hash


def test_gate_fail_blocked_and_not_applicable_are_preserved_without_green_coercion():
    for status in ('pass', 'fail', 'blocked', 'not_applicable'):
        args = dict(schema_version=1, gate_id='G4', status=status, case_ids=(), log_hash='a'*64)
        if status == 'not_applicable':
            args.update(rule_ref='impact/docs-only', input_diff_hash='b'*64, reason='Only prose changed')
        assert GateResult(**args).status == status
    with pytest.raises(ValidationError):
        GateResult(schema_version=1, gate_id='G4', status='not_applicable', case_ids=(), log_hash='a'*64)
    for status in ('skipped', 'xfail'):
        case = GateCaseResult(schema_version=1, case_id='case/fixture', status=status, log_hash='a'*64)
        assert case.status == status
    with pytest.raises(ValidationError):
        GateResult(schema_version=1, gate_id='G1', status='skipped', case_ids=(), log_hash='a'*64)


@pytest.mark.parametrize('changes', [dict(expires_at=10.0), dict(observed_at=float('nan')),
    dict(required_gates=('G1', 'G1')), dict(actual_case_ids=('a', 'a'))])
def test_gate_rejects_ambiguous_sets_and_invalid_expiry(changes):
    with pytest.raises(ValidationError):
        GateManifest(**dict(gate_data(), **changes))


def test_nested_gate_results_and_review_findings_reject_unknown_fields():
    data = gate_data()
    data['results'] = (dict(schema_version=1, gate_id='G1', status='pass', case_ids=(),
        log_hash='a'*64, trusted=True),)
    with pytest.raises(ValidationError):
        GateManifest(**data)
    with pytest.raises(ValidationError):
        ReviewFinding(schema_version=1, finding_ref='finding/1', severity='blocking',
            summary='Missing case', evidence_refs=('case/fixture',), can_authorize=True)


def test_review_is_independent_and_cannot_substitute_for_publication_authorization():
    review = ReviewRecord(**review_data())
    assert review.result == 'approved'
    assert review.reviewer_kind == 'model'
    with pytest.raises(ValidationError):
        ReviewRecord(**dict(review_data(), reviewer_identity='coder/1'))
    promotion = promotion_data()
    del promotion['authorization_ref']
    with pytest.raises(ValidationError):
        PromotionRecord(**promotion)
    assert PromotionRecord(**promotion_data()).authorization_ref != review.target_auth_ref


@pytest.mark.parametrize('changes', [dict(expected_predecessor_revision=True),
    dict(expected_predecessor_revision=-1), dict(authorization_revision=True),
    dict(expires_at=12.0), dict(created_at=float('inf'))])
def test_promotion_requires_finite_expiry_and_explicit_revision_fence(changes):
    with pytest.raises(ValidationError):
        PromotionRecord(**dict(promotion_data(), **changes))


@pytest.mark.parametrize('post_in', ['required_gates', 'results'])
def test_postactivation_gate_rejects_missing_activation_and_run_binding(post_in):
    data = gate_data()
    if post_in == 'required_gates':
        data['required_gates'] = ('G6.post',)
    else:
        data['results'] = (GateResult(schema_version=1, gate_id='G6.post',
            status='pass', case_ids=(), log_hash='8'*64),)
    with pytest.raises(ValidationError, match='G6.post requires activation_ref and run_ref'):
        GateManifest(**data)


@pytest.mark.parametrize('binding', [dict(activation_ref='activation/1'),
    dict(run_ref='run/2')])
def test_postactivation_gate_rejects_partial_binding(binding):
    with pytest.raises(ValidationError, match='G6.post requires activation_ref and run_ref'):
        GateManifest(**dict(gate_data(), required_gates=('G6.post',), **binding))


@pytest.mark.parametrize('field,new_value', [('activation_ref', 'activation/2'),
    ('run_ref', 'run/3')])
def test_postactivation_input_identity_distinguishes_activation_and_observed_run(field, new_value):
    data = dict(gate_data(), required_gates=('G6.post',),
        activation_ref='activation/1', run_ref='run/2')
    first = GateManifest(**data)
    second = GateManifest(**dict(data, **{field: new_value}))
    assert second.input_hash != first.input_hash
    assert second.manifest_hash != first.manifest_hash
    assert second.candidate_hash == first.candidate_hash
    assert GateManifest.model_validate_json(first.model_dump_json()) == first


def test_preactivation_gate_has_no_dependency_on_activation_or_run_identity():
    manifest = GateManifest(**dict(gate_data(), required_gates=('G5', 'G6.pre')))
    assert manifest.activation_ref is None
    assert manifest.run_ref is None
    assert manifest.matches_candidate(CandidateRevision(**candidate_data()))
    with pytest.raises(ValidationError, match='only G6.post'):
        GateManifest(**dict(gate_data(), required_gates=('G6.pre',),
            activation_ref='activation/1', run_ref='run/2'))
