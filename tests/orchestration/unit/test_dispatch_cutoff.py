"""P11.3 / ORC-056b: submission permits require an open gate and matching generation."""
import pytest

from sandbox_graph.domain.models import RunRecord
from sandbox_graph.persistence.repository import Conflict
from sandbox_graph.services.revisions import RevisionService


def pre(repository):
    RevisionService(repository).create(RunRecord(
        run_id='r', root_thread_id='t', graph_ref='g1', active_plan_ref='p1',
        contract_ref='c', auth_ref='a', lineage_id='l'))


def test_open_gate_permits_once_and_is_idempotent(repository):
    pre(repository)
    with repository.transaction() as tx:
        RevisionService.permit_in(tx, 'r', 'op1', generation=1, scope='g')
        RevisionService.permit_in(tx, 'r', 'op1', generation=1, scope='g')      # idempotent
    with pytest.raises(Conflict):
        with repository.transaction() as tx:
            RevisionService.permit_in(tx, 'r', 'op1', generation=2, scope='g')  # identity conflict


def test_closed_gate_and_generation_mismatch_are_rejected(repository):
    pre(repository)
    with repository.transaction() as tx:
        tx.put('dispatch_gates', 'r', {'open': False, 'generation': 2})
    for generation in (1, 2):                                    # closed gate rejects ordinary
        with pytest.raises(Conflict):
            with repository.transaction() as tx:
                RevisionService.permit_in(tx, 'r', f'op-{generation}', generation=generation, scope='g')


def test_cleanup_permit_needs_a_prior_scope_submission(repository):
    pre(repository)
    with pytest.raises(Conflict):
        with repository.transaction() as tx:
            RevisionService.permit_in(tx, 'r', 'cleanup', generation=1, scope='g', kind='cleanup')
