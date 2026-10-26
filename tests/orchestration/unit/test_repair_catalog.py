"""修复目录边界：不能把模型动作名当作可执行代码。"""
import asyncio
from copy import deepcopy
from dataclasses import replace

import pytest
from pydantic import ValidationError

from sandbox_graph.catalog.repair_actions import (
    RepairActionCatalog, RepairActionInput, RepairActionNotRegistered, RepairActionUnavailable,
    standard_repair_catalog,
)
from sandbox_graph.domain.models import NodeResult
from sandbox_graph.domain.agent_lifecycle import ResourceDispositionReceipt
from sandbox_graph.graphs.executor import NodeExecutor
from sandbox_graph.runtime.leases import LeaseService
from sandbox_graph.runtime.node_agent import NodeReview
from sandbox_graph.runtime.runner import RunnerCollection, RunnerStatus
from tests.orchestration.conftest import FakeClock, MemoryRepository


def _successful_repair(node):
    if node == 'N01':
        return NodeResult(execution_status='succeeded', operation_refs=('operation:original',))
    if node == 'N07':
        return NodeResult(execution_status='succeeded', data_status='valid', artifact_refs=('raw:original',))
    return NodeResult(execution_status='succeeded', cleanup_status='complete',
                      operation_refs=('operation:original',), resource_refs=('resource:1',))


def _postcheck_proofs(node):
    if node == 'N01':
        return {'operation_statuses': {'operation:original': RunnerStatus(state='completed', authoritative=True)}}
    if node == 'N07':
        return {'collection': RunnerCollection(events=({'seq': 1},), last_seq=1,
                                               artifact_refs=('raw:original',))}
    return {'checked_at': 1001.0, 'disposition': ResourceDispositionReceipt(
        schema_version=1, work_ref='work:1', operation_refs=('operation:original',),
        stopped=True, admission_status='closed', sampler_status='stopped',
        owned_resources=('resource:1',), residual_resources=(), isolation_domains=(),
        observed_at=1000.0, expires_at=1100.0, clock='wall',
        producer='lifecycle_safety', producer_version='1')}


@pytest.mark.parametrize('action,version,node', [
    ('resource.cleanup', '2', 'N09'), ('resource.cleanup', '1', 'N01'),
    ('case.reconcile', '1', 'N07'), ('__import__("os").system', '1', 'N09'),
    ('resource.cleanup', '1', 'unknown'), ('resource.cleanup', 1, 'N09'),
])
def test_exact_version_action_and_node_are_required(action, version, node):
    with pytest.raises(RepairActionNotRegistered):
        standard_repair_catalog().resolve(action, version, node)


@pytest.mark.parametrize('node,action', [
    ('N02', 'fixture.repair'), ('N03', 'preflight.recheck'),
    ('N04', 'observation.repair'), ('N05', 'resume_owned_operation'),
    ('N06', 'steady.reobserve'), ('N08', 'analysis.recompute'),
    ('N10', 'delivery.republish'),
])
def test_planned_action_is_explicitly_unavailable(node, action):
    entry = standard_repair_catalog().resolve(action, '1', node)
    assert entry.availability == 'unavailable'
    assert entry.executor_route is None
    assert entry.unavailable_reason
    with pytest.raises(RepairActionUnavailable):
        entry.require_available()


def test_all_45_node_policies_allow_diagnosis_without_granting_unimplemented_actions():
    catalog = standard_repair_catalog()
    expected = {
        'R0', 'R1', 'RP', 'R2', 'R3', 'R6', 'R7',
        'G0', 'T0', 'T1', 'T2', 'T4', 'T5', 'T6', 'T8', 'TC', 'TB',
        'GB', 'GW', 'GE', 'E1', 'E2', 'C1', 'C2', 'C3',
        'N00', 'N01', 'N02', 'N03', 'N04', 'N05', 'N06', 'N07', 'N08', 'N09', 'N10',
        'A1', 'A2', 'A3', 'A4', 'A5', 'A7', 'F1', 'F2', 'F4',
    }
    assert set(catalog.node_policies) == expected
    executable = {}
    for node, policy in catalog.node_policies.items():
        assert policy.node_id == node and policy.diagnosis_allowed and policy.candidate_allowed
        assert policy.policy_version == '1'
        for action in policy.actions:
            definition = catalog.resolve(action.action_id, action.version, node)
            if definition.availability == 'available':
                executable[node] = action.action_id
    assert executable == {'N01': 'case.reconcile', 'N07': 'evidence.seal', 'N09': 'resource.cleanup'}
    assert catalog.node_policies['A1'].scope_kind == 'run'
    assert catalog.node_policies['E1'].scope_kind == 'group'
    with pytest.raises(TypeError):
        catalog.node_policies['N05'] = catalog.node_policies['N09']


@pytest.mark.parametrize('changes', [
    {'schema_version': True}, {'schema_version': '1'}, {'schema_version': 2},
    {'evidence_refs': ()}, {'source_receipt': ''}, {'input_refs': {'x': 3}},
    {'code': 'delete_everything()'},
])
def test_action_input_rejects_missing_evidence_and_arbitrary_code(changes):
    body = {'schema_version': 1, 'source_receipt': 'failure:1',
            'evidence_refs': ('evidence:1',), 'input_refs': {'contract': 'contract:1'}}
    with pytest.raises(ValidationError):
        RepairActionInput.model_validate({**body, **changes})


def test_action_input_requires_explicit_schema_version():
    with pytest.raises(ValidationError):
        RepairActionInput(source_receipt='failure:1', evidence_refs=('evidence:1',))


@pytest.mark.parametrize('node,action,effect', [
    ('N01', 'case.reconcile', 'external_read'),
    ('N07', 'evidence.seal', 'artifact_write'),
    ('N09', 'resource.cleanup', 'external_mutation'),
])
def test_available_route_executes_only_bound_node_and_preserves_old_evidence(node, action, effect):
    repo = MemoryRepository(FakeClock())
    token = LeaseService(repo).acquire_thread('repair-thread', 'worker', 100)
    with repo.transaction() as tx:
        tx.put('receipts', 'old:failure', {'result': {'execution_status': 'failed'},
                                          'raw': 'original bytes'}, immutable=True)
    original = deepcopy(repo.rows)

    async def repair(context):
        assert context.node_id == node
        assert context.result_refs == {'raw': 'raw:original'}
        return _successful_repair(node)

    async def forbidden(context):
        pytest.fail('a repair route invoked another node')

    handlers = {node: repair, 'N05': forbidden}
    executor = NodeExecutor(repo, handlers, token)
    entry = standard_repair_catalog().resolve(action, '1', node).require_available()
    assert entry.effect_class == effect
    assert entry.permissions and entry.conflict_domains
    assert entry.postcheck_ref and entry.reconcile_ref and entry.cleanup_ref
    assert entry.verification_status == 'locally_verified'
    state = {'thread_id': 'repair-thread', 'scope': 'work:1', 'run_ref': 'run:1',
             'plan_ref': 'plan:1', 'graph_ref': 'graph:1', 'activation_id': 'new-attempt',
             'input_refs': {}, 'result_refs': {'raw': 'raw:original'}}
    result, receipt = asyncio.run(entry.executor_route.execute(executor, state))
    with repo.transaction() as tx:
        review = NodeReview.model_validate(tx.get('node_reviews', receipt)['body'])
        assert tx.get('receipts', 'old:failure') == original[('receipts', 'old:failure')]
        assert tx.get('receipts', receipt)['body']['identity']['node_id'] == node
    assert entry.postcheck(result, review, **_postcheck_proofs(node))


def test_missing_real_node_handler_is_unavailable_before_executor_writes():
    repo = MemoryRepository(FakeClock())
    token = LeaseService(repo).acquire_thread('repair-thread', 'worker', 100)
    entry = standard_repair_catalog().resolve('resource.cleanup', '1', 'N09')
    with pytest.raises(RepairActionUnavailable):
        asyncio.run(entry.executor_route.execute(NodeExecutor(repo, {}, token), {}))
    with repo.transaction() as tx:
        assert tx.scan('visits') == [] and tx.scan('receipts') == []


def test_unavailable_definition_cannot_pass_postcheck():
    entry = standard_repair_catalog().resolve('fixture.repair', '1', 'N02')
    review = NodeReview(node_id='N02', owner_agent_id='agent:work:1', decision='accept')
    assert entry.postcheck(NodeResult(execution_status='succeeded'), review) is False


@pytest.mark.parametrize('changes', [
    {'input_schema': dict}, {'postcheck_ref': ''}, {'reconcile_ref': ''},
    {'cleanup_ref': ''}, {'permissions': ()}, {'conflict_domains': ()},
])
def test_incomplete_available_registration_cannot_be_enabled(changes):
    entry = standard_repair_catalog().resolve('resource.cleanup', '1', 'N09')
    with pytest.raises(ValueError):
        RepairActionCatalog((replace(entry, **changes),), ())


def test_empty_policy_reports_repair_unavailable_without_disabling_diagnosis():
    policy = standard_repair_catalog().node_policies['R0']
    assert policy.repair_availability == 'unavailable'
    assert policy.unavailable_reason == 'no_registered_repair_action'
    assert policy.diagnosis_allowed and policy.candidate_allowed


@pytest.mark.parametrize('status,review_status,review_node,decision,want', [
    ('succeeded', 'accepted', 'N09', 'accept', True),
    ('failed', 'accepted', 'N09', 'accept', False),
    ('succeeded', 'rejected', 'N09', 'accept', False),
    ('succeeded', 'accepted', 'N09', 'reject', False),
    ('succeeded', 'accepted', 'N01', 'accept', False),
])
def test_postcheck_requires_matching_independent_review(status, review_status, review_node, decision, want):
    entry = standard_repair_catalog().resolve('resource.cleanup', '1', 'N09')
    result = _successful_repair('N09').model_copy(update={'execution_status': status, 'review_status': review_status})
    review = NodeReview(node_id=review_node, owner_agent_id='agent:work:work:1', decision=decision)
    assert entry.postcheck(result, review, **_postcheck_proofs('N09')) is want
    assert entry.postcheck(result, None) is False


@pytest.mark.parametrize('node,action', [('N01', 'case.reconcile'), ('N07', 'evidence.seal'), ('N09', 'resource.cleanup')])
def test_generic_accept_cannot_replace_action_specific_evidence(node, action):
    result = _successful_repair(node)
    review = NodeReview(node_id=node, owner_agent_id='agent:work:work:1', decision='accept')
    assert standard_repair_catalog().resolve(action, '1', node).postcheck(result, review) is False


@pytest.mark.parametrize('node,action,result_changes,proof_changes', [
    ('N09', 'resource.cleanup', {'cleanup_status': 'unknown'}, {}),
    ('N09', 'resource.cleanup', {'cleanup_status': 'incomplete'}, {}),
    ('N09', 'resource.cleanup', {'cleanup_status': 'not_needed'}, {}),
    ('N07', 'evidence.seal', {'data_status': 'insufficient'}, {}),
    ('N07', 'evidence.seal', {'data_status': 'invalid'}, {}),
    ('N07', 'evidence.seal', {}, {'collection': RunnerCollection(missing_sequences=(1,), last_seq=1, artifact_refs=('raw:original',))}),
    ('N07', 'evidence.seal', {}, {'collection': RunnerCollection(interrupted=True, artifact_refs=('raw:original',))}),
    ('N01', 'case.reconcile', {}, {'operation_statuses': {'operation:original': RunnerStatus(state='unknown', authoritative=True)}}),
    ('N01', 'case.reconcile', {}, {'operation_statuses': {'operation:original': RunnerStatus(state='completed', authoritative=False)}}),
    ('N01', 'case.reconcile', {}, {'operation_statuses': {'operation:other': RunnerStatus(state='completed', authoritative=True)}}),
])
def test_real_node_review_accept_does_not_override_unresolved_action(node, action, result_changes, proof_changes):
    repo = MemoryRepository(FakeClock())
    token = LeaseService(repo).acquire_thread('repair-thread', 'worker', 100)

    async def repair(context):
        return _successful_repair(node).model_copy(update=result_changes)

    executor = NodeExecutor(repo, {node: repair}, token)
    state = {'thread_id': 'repair-thread', 'scope': 'work:1', 'run_ref': 'run:1',
             'plan_ref': 'plan:1', 'graph_ref': 'graph:1', 'activation_id': 'new-attempt', 'input_refs': {}}
    result, receipt = asyncio.run(executor.execute(node, state))
    with repo.transaction() as tx:
        review = NodeReview.model_validate(tx.get('node_reviews', receipt)['body'])
    assert review.decision == 'accept'
    proofs = {**_postcheck_proofs(node), **proof_changes}
    assert standard_repair_catalog().resolve(action, '1', node).postcheck(result, review, **proofs) is False


@pytest.mark.parametrize('proof_changes,checked_at', [
    ({'residual_resources': ('resource:1',)}, 1001.0),
    ({'isolation_domains': ('domain:quarantined',)}, 1001.0),
    ({'stopped': False}, 1001.0), ({'admission_status': 'open'}, 1001.0),
    ({'sampler_status': 'running'}, 1001.0), ({'sampler_status': 'unknown'}, 1001.0),
    ({'work_ref': 'another-work'}, 1001.0),
    ({'operation_refs': ('operation:another',)}, 1001.0),
    ({'owned_resources': ('resource:another',)}, 1001.0),
    ({'producer': 'model'}, 1001.0), ({'producer_version': '2'}, 1001.0),
    ({}, 999.0), ({}, 1100.0), ({}, None), ({}, True), ({}, float('nan')),
])
def test_cleanup_requires_current_closed_disposition_for_same_work_and_inventory(proof_changes, checked_at):
    entry = standard_repair_catalog().resolve('resource.cleanup', '1', 'N09')
    review = NodeReview(node_id='N09', owner_agent_id='agent:work:work:1', decision='accept')
    disposition = _postcheck_proofs('N09')['disposition'].model_copy(update=proof_changes)
    assert entry.postcheck(_successful_repair('N09'), review,
                           disposition=disposition, checked_at=checked_at) is False
