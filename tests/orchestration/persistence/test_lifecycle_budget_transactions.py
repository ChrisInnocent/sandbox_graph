"""ALI-03: independent PostgreSQL contenders reserve the final shared allowance."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import pytest
from sandbox_graph.persistence.repository import Conflict
from sandbox_graph.services.lifecycle_budgets import LifecycleBudgetService
from tests.orchestration.unit.test_lifecycle_budgets import LIMITS,SAFETY

pytestmark=pytest.mark.postgres


def test_last_lineage_allowance_has_one_winner_across_lifecycles(postgres):
    repository,_=postgres
    with repository.transaction() as tx:
        for life in ('first','second'):
            LifecycleBudgetService.initialize_in(tx,lifecycle_id=life,lineage_budget_ref='shared',
                limits=LIMITS|{'repair_attempts':1},deadline=tx.now+60,safety_limits=SAFETY,
                safety_deadline=tx.now+120,cost_unit='microUSD')
    ready=Barrier(2)
    def compete(life):
        ready.wait(timeout=10)
        try:
            with repository.transaction() as tx:
                LifecycleBudgetService.reserve_in(tx,request_id=life,lifecycle_id=life,input_ref=life,amounts={'repair_attempts':1})
            return 'accepted'
        except Conflict:return 'rejected'
    with ThreadPoolExecutor(max_workers=2) as workers:
        assert sorted(workers.map(compete,('first','second')))==['accepted','rejected']
    with repository.transaction() as tx:
        assert LifecycleBudgetService.snapshot_in(tx,'first')['lineage']['reserved']['repair_attempts']==1
        assert sum(LifecycleBudgetService.snapshot_in(tx,life)['lifecycle']['reserved']['repair_attempts'] for life in ('first','second'))==1
