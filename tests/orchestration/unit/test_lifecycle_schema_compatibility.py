"""Literal historical inputs stay readable, immutable and powerless."""
from copy import deepcopy
import json

import pytest
from pydantic import ValidationError

from sandbox_graph.domain.agent_lifecycle import (
    ContextPacket, LifecycleEvent, LifecycleRecord, TransitionGuards, transition_lifecycle,
)
from sandbox_graph.domain.evolution import ChangeProposal
from sandbox_graph.persistence.repository import Conflict
from sandbox_graph.services.agent_lifecycle import LifecycleService
from sandbox_graph.services.lifecycle_budgets import LifecycleBudgetService
from sandbox_graph.services.remediation import NodeRemediationService


@pytest.fixture
def old_context_wire(lifecycle_record_wire):
    return {'schema_version': 1, 'key': lifecycle_record_wire['key'],
        'node_supervisor_id': 'supervisor/N01', 'task_role': 'diagnosis',
        'graph_ref': 'graph/a', 'plan_ref': 'plan/a', 'contract_ref': 'contract/a',
        'failure_summary': 'Fixture absent', 'facts': ['fixture absent'], 'hypotheses': [],
        'evidence_refs': ['receipt/historical'], 'evidence_scope_refs': ['work/a'],
        'evidence_byte_limit': 256, 'budget_usage': {'schema_version': 1,
            'repair_attempts': 2, 'diagnosis_calls': 1, 'tokens_reserved': 70},
        'disposition_receipt_ref': 'disposition/historical'}


@pytest.fixture
def change_proposal_wire(lifecycle_record_wire):
    return {'schema_version': 1, 'proposal_id': 'proposal/a',
        'source_key': lifecycle_record_wire['key'], 'base_code_ref': 'code/a',
        'base_graph_ref': 'graph/a', 'failure_evidence_refs': ['receipt/historical'],
        'facts': ['fixture absent'], 'hypotheses': [], 'target_behavior': 'Restore fixture',
        'allowed_paths': ['src/adapter.py'], 'node_scope': ['N01'], 'contract_scope': [],
        'acceptance_refs': ['case/fixture'], 'scope_auth': 'decision/scope'}


def test_read_only_load_preserves_historical_record_bytes_and_structure(
        repository, tmp_path, lifecycle_record_wire, historical_record_bytes):
    raw = (json.dumps(lifecycle_record_wire, ensure_ascii=False, indent=1) + '\n').encode()
    # Use a literal old wire file as the boundary; reads must never serialize it back.
    path = tmp_path / 'historical.json'
    path.write_bytes(raw)
    with repository.transaction() as tx:
        tx.put('agent_lifecycles', 'life/historical', json.loads(path.read_bytes()), immutable=True)
        for (kind, key), body in historical_record_bytes.items():
            tx.put(kind, key, json.loads(body), immutable=True)
    before = deepcopy(repository.rows)
    with repository.transaction() as tx:
        record = LifecycleService.load_in(tx, 'life/historical')
        assert record.phase == 'diagnosing'
        assert record.revision == 7
        assert record.attempt_refs == ('attempt/1', 'attempt/2')
        assert record.stop_reason is None and record.operation_refs == ()
        assert record.node_result.performance_status == 'fail'
        tx.get('remedy_orders', 'legacy/order')['body']['attempt'] = 0
    assert repository.rows == before
    assert path.read_bytes() == raw


def test_historical_record_accepts_new_event_without_rewriting_old_result_or_attempts(
        lifecycle_record_wire, lifecycle_failure_event):
    original = LifecycleRecord.model_validate_json(json.dumps(lifecycle_record_wire))
    event = LifecycleEvent.model_validate_json(json.dumps(lifecycle_failure_event))
    changed = transition_lifecycle(original, event, TransitionGuards(schema_version=1))
    assert changed.revision == 8
    assert changed.phase == 'diagnosing'
    assert changed.attempt_refs == ('attempt/1', 'attempt/2')
    assert changed.node_result.execution_status == 'failed'
    assert changed.node_result.performance_status == 'fail'
    assert original.revision == 7


@pytest.mark.parametrize('model,fixture_name', [
    (LifecycleRecord, 'lifecycle_record_wire'), (LifecycleEvent, 'lifecycle_failure_event'),
    (ContextPacket, 'old_context_wire'), (ChangeProposal, 'change_proposal_wire')],
    ids=['record', 'event', 'context', 'evolution-proposal'])
@pytest.mark.parametrize('patch', [{'schema_version': 2}, {'schema_version': True},
    {'schema_version': '1'}, {'authorization_valid': True}, {'resources_disposed': True}],
    ids=['future-version', 'bool-version', 'string-version', 'unknown-authority', 'unknown-disposition'])
def test_new_contracts_reject_future_versions_and_unknown_grants(request, model, fixture_name, patch):
    wire = request.getfixturevalue(fixture_name)
    with pytest.raises(ValidationError):
        model.model_validate_json(json.dumps(wire | patch))
    assert model.model_validate_json(json.dumps(wire)).schema_version == 1


def test_old_context_defaults_to_live_and_retains_historical_usage(old_context_wire):
    original = deepcopy(old_context_wire)
    packet = ContextPacket.model_validate_json(json.dumps(old_context_wire))
    assert packet.evidence_mode == 'live'
    assert packet.budget_usage.repair_attempts == 2
    assert packet.budget_usage.diagnosis_calls == 1
    assert packet.budget_usage.tokens_reserved == 70
    assert packet.tool_capabilities == ()
    assert old_context_wire == original


@pytest.mark.parametrize('patch', [
    {'evidence_mode': 'offline_sealed'},
    {'evidence_mode': 'offline_sealed', 'disposition_receipt_ref': None,
        'tool_capabilities': ['site.submit']},
    {'disposition_receipt_ref': None}],
    ids=['offline-with-live-proof', 'offline-with-tool', 'implicit-offline'])
def test_offline_context_requires_explicit_mode_and_has_no_live_authority(old_context_wire, patch):
    with pytest.raises(ValidationError):
        ContextPacket.model_validate_json(json.dumps(old_context_wire | patch))


def test_explicit_offline_context_keeps_sealed_evidence_without_live_capabilities(old_context_wire):
    packet = ContextPacket.model_validate_json(json.dumps(old_context_wire |
        {'evidence_mode': 'offline_sealed', 'disposition_receipt_ref': None}))
    assert packet.evidence_mode == 'offline_sealed'
    assert packet.disposition_receipt_ref is None
    assert packet.tool_capabilities == ()
    assert packet.evidence_refs == ('receipt/historical',)
    assert packet.budget_usage.repair_attempts == 2


def test_loading_and_reinitializing_historical_budgets_never_reset_spent_counters(
        historical_budget_repository, lifecycle_budget_config):
    repository = historical_budget_repository
    before = deepcopy(repository.rows)
    with repository.transaction() as tx:
        snapshot = LifecycleBudgetService.initialize_in(tx, **lifecycle_budget_config)
        assert snapshot['lifecycle']['spent']['repair_attempts'] == 2
        assert snapshot['lineage']['spent']['tokens'] == 70
        assert snapshot['lifecycle']['safety_spent']['stop_calls'] == 1
    assert repository.rows == before
    with repository.transaction() as tx, pytest.raises(Conflict, match='exhausted'):
        LifecycleBudgetService.reserve_in(tx, request_id='third-attempt',
            lifecycle_id='life/historical', amounts={'repair_attempts': 1}, input_ref='input/new')
    assert repository.rows == before


@pytest.mark.asyncio
async def test_legacy_reviews_and_order_api_cannot_create_new_jobs_or_authorizations(
        repository, historical_record_bytes, lifecycle_model):
    from sandbox_graph.runtime.agents import AgentGateway, ContextBudget
    with repository.transaction() as tx:
        for (kind, key), raw in historical_record_bytes.items():
            tx.put(kind, key, json.loads(raw), immutable=True)
    before = deepcopy(repository.rows)
    gateway = AgentGateway(repository, lifecycle_model, registered_actions=('case.reconcile',))
    context = gateway.build_context(scope='work/a', node_id='N01', fields={}, allowed_fields=set(),
        budget=ContextBudget(max_input_bytes=512, max_output_bytes=1024,
            max_calls=1, max_tokens=10, deadline=1100.0))
    proposal = await gateway.diagnose(context, accessible_evidence={'receipt/historical'}, resources_disposed=True)
    result = NodeRemediationService(repository).order(owner_agent_id='agent:work:work/a',
        work_ref='work/a', node_id='N01', run_ref='run/a', plan_ref='plan/a', graph_ref='graph/a',
        decision={'status': 'proposed', 'proposal': proposal.model_dump(mode='json')}, deadline=1100.0)
    assert result['status'] == 'blocked'
    assert result['reason'] == 'lifecycle_contract_required'
    with repository.transaction() as tx:
        for kind in ('jobs', 'agent_lifecycles', 'repair_authorizations', 'repair_requests', 'repair_orders'):
            assert tx.scan(kind) == []
        with pytest.raises(KeyError):
            LifecycleService.load_in(tx, 'legacy/order')
    for identity, row in before.items():
        assert repository.rows[identity] == row
