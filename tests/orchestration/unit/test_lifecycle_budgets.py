"""ALU-04/05: synthetic repository tests, not PostgreSQL qualification."""
from concurrent.futures import ThreadPoolExecutor

import pytest

from sandbox_graph.persistence.repository import Conflict
from sandbox_graph.services.lifecycle_budgets import LifecycleBudgetService


LIMITS = {'repair_attempts': 2, 'diagnosis_calls': 3, 'candidate_revisions': 2,
          'tokens': 100, 'child_jobs': 3, 'observations': 4, 'cost': 1000}
SAFETY = {'stop_calls': 2, 'query_calls': 3, 'cleanup_calls': 2, 'cost': 50}


def service(repository):
    result = LifecycleBudgetService(repository)
    with repository.transaction() as tx:
        result.initialize_in(tx, lifecycle_id='life', lineage_budget_ref='lineage',
                             limits=LIMITS, deadline=1100.0, safety_limits=SAFETY,
                             safety_deadline=1200.0, cost_unit='microUSD')
    return result


def reserve(budgets, repository, request_id, **kwargs):
    with repository.transaction() as tx:
        return budgets.reserve_in(tx, request_id=request_id, lifecycle_id='life',
                                  input_ref='input:1', **kwargs)


def snapshot(budgets, repository, lifecycle_id='life'):
    with repository.transaction() as tx:
        return budgets.snapshot_in(tx, lifecycle_id)


def test_last_allowed_attempt_can_succeed(repository):
    budgets = service(repository)
    for request_id in ('failed-first', 'successful-last'):
        reserve(budgets, repository, request_id, amounts={'repair_attempts': 1})
        with repository.transaction() as tx:
            budgets.settle_in(tx, request_id=request_id)
    assert snapshot(budgets, repository)['lifecycle']['spent']['repair_attempts'] == 2
    with pytest.raises(Conflict, match='exhausted'):
        reserve(budgets, repository, 'third', amounts={'repair_attempts': 1})
    assert snapshot(budgets, repository)['lineage']['spent']['repair_attempts'] == 2


def test_budget_survives_context_and_revision_changes(repository):
    budgets = service(repository)
    reserve(budgets, repository, 'model:1', amounts={'diagnosis_calls': 1, 'tokens': 70, 'cost': 700},
            provider_enforces_maximum=True,
            metadata={'context': 'old', 'model': 'm1', 'policy': 'p1', 'revision': 1, 'run': 'r1'})
    # Reconstructed services read durable accounting, including unknown usage.
    budgets = LifecycleBudgetService(repository)
    with repository.transaction() as tx:
        receipt = budgets.settle_in(tx, request_id='model:1')
    assert receipt['usage_status'] == 'unknown'
    assert snapshot(budgets, repository)['lifecycle']['spent']['tokens'] == 70
    with repository.transaction() as tx:
        budgets.initialize_in(tx, lifecycle_id='successor', lineage_budget_ref='lineage',
                              limits=LIMITS, deadline=1100.0, safety_limits=SAFETY,
                              safety_deadline=1200.0, cost_unit='microUSD')
    with pytest.raises(Conflict, match='exhausted'):
        with repository.transaction() as tx:
            budgets.reserve_in(tx, request_id='model:2', lifecycle_id='successor', input_ref='changed',
                               amounts={'diagnosis_calls': 1, 'tokens': 40, 'cost': 100},
                               provider_enforces_maximum=True,
                               metadata={'context': 'new', 'model': 'm2', 'policy': 'p2', 'revision': 2, 'run': 'r2'})
    assert snapshot(budgets, repository, 'successor')['lineage']['spent']['tokens'] == 70
    with pytest.raises(Conflict):
        with repository.transaction() as tx:
            budgets.initialize_in(tx, lifecycle_id='life', lineage_budget_ref='new-lineage',
                                  limits=LIMITS, deadline=1100.0, safety_limits=SAFETY,
                                  safety_deadline=1200.0, cost_unit='microUSD')


def test_lifecycle_and_lineage_reserve_atomically_under_concurrency(repository):
    budgets = service(repository)
    def claim(index):
        try:
            reserve(budgets, repository, f'repair:{index}', amounts={'repair_attempts': 1})
            return 'accepted'
        except Conflict:
            return 'blocked'
    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(claim, range(12)))
    assert outcomes.count('accepted') == 2
    totals = snapshot(budgets, repository)
    assert totals['lifecycle']['reserved']['repair_attempts'] == 2
    assert totals['lineage']['reserved']['repair_attempts'] == 2
    assert totals['lifecycle']['reserved']['child_jobs'] == 0


def test_request_replay_is_content_bound_and_settlement_releases_only_unused_maximum(repository):
    budgets = service(repository)
    args = dict(amounts={'diagnosis_calls': 1, 'tokens': 60, 'cost': 600}, provider_enforces_maximum=True)
    first = reserve(budgets, repository, 'call', **args)
    assert reserve(budgets, repository, 'call', **args) == first
    with pytest.raises(Conflict):
        reserve(budgets, repository, 'call', metadata={'context': 'different'}, **args)
    with repository.transaction() as tx:
        settled = budgets.settle_in(tx, request_id='call', actual_tokens=20, actual_cost=150)
        assert budgets.settle_in(tx, request_id='call', actual_tokens=20, actual_cost=150) == settled
    totals = snapshot(budgets, repository)
    assert totals['lifecycle']['spent']['tokens'] == 20
    assert totals['lineage']['spent']['cost'] == 150
    assert totals['lifecycle']['spent']['diagnosis_calls'] == 1
    assert totals['lifecycle']['reserved']['tokens'] == 0
    with pytest.raises(Conflict):
        with repository.transaction() as tx:
            budgets.settle_in(tx, request_id='call', actual_tokens=0, actual_cost=0)


def test_model_rejects_unenforceable_maximum_and_overreported_usage(repository):
    budgets = service(repository)
    with pytest.raises(ValueError):
        reserve(budgets, repository, 'unsafe', amounts={'diagnosis_calls': 1, 'tokens': 50, 'cost': 100})
    reserve(budgets, repository, 'bounded', amounts={'diagnosis_calls': 1, 'tokens': 50, 'cost': 100},
            provider_enforces_maximum=True)
    with pytest.raises(Conflict):
        with repository.transaction() as tx:
            budgets.settle_in(tx, request_id='bounded', actual_tokens=51, actual_cost=100)
    assert snapshot(budgets, repository)['lifecycle']['reserved']['tokens'] == 50


def test_distinct_counters_and_outer_transaction_rollback(repository):
    budgets = service(repository)
    reserve(budgets, repository, 'observe', amounts={'observations': 1})
    reserve(budgets, repository, 'candidate', amounts={'candidate_revisions': 1, 'child_jobs': 1})
    with pytest.raises(RuntimeError):
        with repository.transaction() as tx:
            budgets.reserve_in(tx, request_id='rolled-back', lifecycle_id='life', input_ref='i',
                               amounts={'repair_attempts': 1})
            tx.put('orders', 'oops', {'status': 'queued'})
            raise RuntimeError('order creation failed')
    totals = snapshot(budgets, repository)['lifecycle']
    assert totals['reserved']['observations'] == 1
    assert totals['reserved']['candidate_revisions'] == 1
    assert totals['reserved']['child_jobs'] == 1
    assert totals['reserved']['repair_attempts'] == 0
    with repository.transaction() as tx:
        assert tx.get('orders', 'oops') is None


def test_authorized_adjustments_append_without_erasing_history(repository):
    budgets = service(repository)
    reserve(budgets, repository, 'first', amounts={'repair_attempts': 2})
    with repository.transaction() as tx:
        budgets.settle_in(tx, request_id='first')
        tx.put('budget_authorizations', 'approval', {
            'target_kind': 'lifecycle', 'target_ref': 'life', 'additions': {'repair_attempts': 1},
            'cost_unit': 'microUSD', 'expires_at': 1050.0, 'revoked': False, 'safety': False}, immutable=True)
        kwargs = dict(adjustment_id='add:1', target_kind='lifecycle', target_ref='life',
                      additions={'repair_attempts': 1}, authorization_ref='approval')
        receipt = budgets.adjust_in(tx, **kwargs)
        assert budgets.adjust_in(tx, **kwargs) == receipt
    totals = snapshot(budgets, repository)['lifecycle']
    assert totals['limits']['repair_attempts'] == 3
    assert totals['spent']['repair_attempts'] == 2
    # Lifecycle authority cannot silently enlarge the independent lineage cap.
    with pytest.raises(Conflict, match='exhausted'):
        reserve(budgets, repository, 'still-lineage-blocked', amounts={'repair_attempts': 1})
    with repository.transaction() as tx:
        assert len(tx.scan('budget_adjustments')) == 1
    with pytest.raises(Conflict):
        with repository.transaction() as tx:
            budgets.adjust_in(tx, adjustment_id='reused-authority', target_kind='lifecycle', target_ref='life',
                              additions={'repair_attempts': 1}, authorization_ref='approval')


def test_safety_envelope_is_separate_bounded_and_cannot_fund_diagnosis(repository, clock):
    budgets = service(repository)
    reserve(budgets, repository, 'all-repairs', amounts={'repair_attempts': 2})
    clock.advance(101)
    with pytest.raises(Conflict, match='deadline'):
        reserve(budgets, repository, 'late', amounts={'observations': 1})
    reserve(budgets, repository, 'stop', amounts={'stop_calls': 1, 'cost': 30},
            safety=True, operation_kind='stop')
    with pytest.raises(ValueError):
        reserve(budgets, repository, 'smuggled-diagnosis', amounts={'diagnosis_calls': 1, 'cost': 1},
                safety=True, operation_kind='query')
    with pytest.raises(Conflict, match='exhausted'):
        reserve(budgets, repository, 'over-safety', amounts={'query_calls': 1, 'cost': 21},
                safety=True, operation_kind='query')
    clock.advance(100)
    with pytest.raises(Conflict, match='deadline'):
        reserve(budgets, repository, 'expired-safety', amounts={'cleanup_calls': 1},
                safety=True, operation_kind='cleanup')
    totals = snapshot(budgets, repository)
    assert totals['lifecycle']['safety_reserved']['cost'] == 30
    assert totals['lineage']['safety_reserved']['cost'] == 30


@pytest.mark.parametrize('amounts', [{'repair_attempts': True}, {'tokens': -1}, {'unknown': 1}, {}, {'tokens': 0}])
def test_invalid_reservations_never_mutate_accounting(repository, amounts):
    budgets = service(repository)
    with pytest.raises(ValueError):
        reserve(budgets, repository, 'invalid', amounts=amounts)
    assert snapshot(budgets, repository)['lifecycle']['reserved']['tokens'] == 0



def test_shared_lineage_limit_serializes_different_lifecycles(repository):
    budgets = service(repository)
    with repository.transaction() as tx:
        budgets.initialize_in(tx, lifecycle_id='other-life', lineage_budget_ref='lineage',
                              limits={**LIMITS, 'repair_attempts': 10}, deadline=1150.0,
                              safety_limits=SAFETY, safety_deadline=1250.0, cost_unit='microUSD')
    def claim(index):
        try:
            with repository.transaction() as tx:
                budgets.reserve_in(tx, request_id=f'shared:{index}',
                                   lifecycle_id='life' if index % 2 else 'other-life',
                                   input_ref='i', amounts={'repair_attempts': 1})
            return True
        except Conflict:
            return False
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert list(pool.map(claim, range(12))).count(True) == 2
    assert budgets.snapshot('life')['lineage']['reserved']['repair_attempts'] == 2


def test_lifecycle_denial_does_not_leak_lineage_reservation_when_conflict_is_caught(repository):
    budgets = service(repository)
    with repository.transaction() as tx:
        budgets.initialize_in(tx, lifecycle_id='small', lineage_budget_ref='wide-lineage',
                              limits={**LIMITS, 'repair_attempts': 1}, deadline=1100.0,
                              safety_limits=SAFETY, safety_deadline=1200.0, cost_unit='microUSD',
                              lineage_limits={**LIMITS, 'repair_attempts': 5})
        budgets.reserve_in(tx, request_id='small:1', lifecycle_id='small', input_ref='i',
                           amounts={'repair_attempts': 1})
        with pytest.raises(Conflict, match='lifecycle budget exhausted'):
            budgets.reserve_in(tx, request_id='small:2', lifecycle_id='small', input_ref='i',
                               amounts={'repair_attempts': 1})
        tx.put('continuing-transaction', 'allowed', {'saved': True})
    assert budgets.snapshot('small')['lineage']['reserved']['repair_attempts'] == 1


def test_expired_reservation_can_settle_but_request_identity_cannot_migrate(repository, clock):
    budgets = service(repository)
    budgets.reserve(request_id='pending', lifecycle_id='life', input_ref='i',
                    amounts={'diagnosis_calls': 1, 'tokens': 50, 'cost': 200},
                    provider_enforces_maximum=True)
    clock.advance(500)
    budgets.settle(request_id='pending', actual_tokens=10)
    totals = budgets.snapshot('life')
    assert totals['lifecycle']['spent']['tokens'] == 10
    assert totals['lifecycle']['spent']['cost'] == 200
    # Replay is safe after deadlines because no new dispatch is authorized here.
    assert budgets.reserve(request_id='pending', lifecycle_id='life', input_ref='i',
                           amounts={'diagnosis_calls': 1, 'tokens': 50, 'cost': 200},
                           provider_enforces_maximum=True)['request_id'] == 'pending'
    with pytest.raises(Conflict):
        budgets.reserve(request_id='pending', lifecycle_id='successor', input_ref='i',
                        amounts={'diagnosis_calls': 1, 'tokens': 50, 'cost': 200},
                        provider_enforces_maximum=True)


def test_authority_requires_explicit_safety_binding(repository):
    budgets = service(repository)
    with repository.transaction() as tx:
        tx.put('budget_authorizations', 'unbound', {
            'target_kind': 'lifecycle', 'target_ref': 'life', 'additions': {'repair_attempts': 1},
            'cost_unit': 'microUSD', 'expires_at': 1050.0, 'revoked': False}, immutable=True)
    # A malformed/partial authority receipt must never enlarge business quota.
    with pytest.raises(Conflict):
        budgets.adjust(adjustment_id='missing-safety-binding', target_kind='lifecycle', target_ref='life',
                       additions={'repair_attempts': 1}, authorization_ref='unbound')
    assert budgets.snapshot('life')['lifecycle']['limits']['repair_attempts'] == 2


@pytest.mark.parametrize('grant_patch', [
    {'revoked': True}, {'expires_at': 1000.0}, {'expires_at': float('inf')},
    {'target_ref': 'other'}, {'cost_unit': 'USD'}, {'additions': {'repair_attempts': True}},
    {'safety': True},
])
def test_invalid_authority_cannot_add_budget(repository, grant_patch):
    budgets = service(repository)
    with repository.transaction() as tx:
        tx.put('budget_authorizations', 'bad', {
            'target_kind': 'lifecycle', 'target_ref': 'life', 'additions': {'repair_attempts': 1},
            'cost_unit': 'microUSD', 'expires_at': 1050.0, 'revoked': False, 'safety': False,
            **grant_patch}, immutable=True)
    with pytest.raises(Conflict):
        budgets.adjust(adjustment_id='rejected', target_kind='lifecycle', target_ref='life',
                       additions={'repair_attempts': 1}, authorization_ref='bad')
    assert budgets.snapshot('life')['lifecycle']['limits']['repair_attempts'] == 2


def test_proven_unsubmitted_releases_usage_but_preserves_request_count(repository):
    budgets = service(repository)
    budgets.reserve(request_id='not-sent', lifecycle_id='life', input_ref='i',
                    amounts={'diagnosis_calls': 1, 'tokens': 50, 'cost': 200},
                    provider_enforces_maximum=True)
    receipt = budgets.settle(request_id='not-sent', proven_not_submitted=True)
    assert receipt['usage_status'] == 'not_submitted'
    assert budgets.snapshot('life')['lifecycle']['spent']['tokens'] == 0
    assert budgets.snapshot('life')['lifecycle']['spent']['diagnosis_calls'] == 1


def test_initialization_replay_preserves_adjusted_limits_and_original_identity(repository):
    budgets = service(repository)
    with repository.transaction() as tx:
        tx.put('budget_authorizations', 'add', {
            'target_kind': 'lineage', 'target_ref': 'lineage', 'additions': {'tokens': 20},
            'cost_unit': 'microUSD', 'expires_at': 1050.0, 'revoked': False, 'safety': False}, immutable=True)
    budgets.adjust(adjustment_id='extra', target_kind='lineage', target_ref='lineage',
                   additions={'tokens': 20}, authorization_ref='add')
    budgets.initialize(lifecycle_id='life', lineage_budget_ref='lineage', limits=LIMITS,
                       deadline=1100.0, safety_limits=SAFETY, safety_deadline=1200.0, cost_unit='microUSD')
    totals = budgets.snapshot('life')['lineage']
    assert totals['original']['limits']['tokens'] == 100
    assert totals['limits']['tokens'] == 120


def test_safety_settlement_uses_only_safety_accounting(repository):
    budgets = service(repository)
    budgets.reserve(request_id='cleanup', lifecycle_id='life', input_ref='i', safety=True,
                    operation_kind='cleanup', amounts={'cleanup_calls': 1, 'cost': 30})
    budgets.settle(request_id='cleanup', actual_cost=10)
    totals = budgets.snapshot('life')
    for kind in ('lifecycle', 'lineage'):
        assert totals[kind]['safety_consumed']['cleanup_calls'] == 1
        assert totals[kind]['safety_consumed']['cost'] == 10
        assert totals[kind]['safety_reserved']['cost'] == 0
        assert totals[kind]['consumed']['cost'] == 0
    with pytest.raises(Conflict, match='exhausted'):
        budgets.reserve(request_id='too-expensive-cleanup', lifecycle_id='life', input_ref='i', safety=True,
                        operation_kind='cleanup', amounts={'cleanup_calls': 1, 'cost': 41})


@pytest.mark.parametrize('patch', [
    {'limits': {**LIMITS, 'tokens': True}}, {'limits': {**LIMITS, 'cost': 0}},
    {'deadline': float('inf')}, {'safety_deadline': 1099.0}, {'cost_unit': ''},
    {'lineage_limits': {**LIMITS, 'observations': -1}},
])
def test_invalid_initialization_has_no_partial_ledger(repository, patch):
    budgets = LifecycleBudgetService(repository)
    with repository.transaction() as tx:
        with pytest.raises(ValueError):
            budgets.initialize_in(tx, **{'lifecycle_id': 'life', 'lineage_budget_ref': 'lineage',
                'limits': LIMITS, 'deadline': 1100.0, 'safety_limits': SAFETY,
                'safety_deadline': 1200.0, 'cost_unit': 'microUSD', **patch})
        assert tx.scan('lifecycle_budgets') == []
