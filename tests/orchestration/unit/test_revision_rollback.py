"""P11.5 / ORC-056g,h,055: same-graph plan CAS; graph change creates a new lineage."""
import pytest

from sandbox_graph.domain.models import RunRecord
from sandbox_graph.persistence.repository import Conflict
from sandbox_graph.services.revisions import RevisionService


def pre(repository):
    RevisionService(repository).create(RunRecord(
        run_id='r', root_thread_id='t', graph_ref='g1', active_plan_ref='p1',
        contract_ref='c', auth_ref='a', lineage_id='l'))


def activate(repository, **kwargs):
    args = dict(request_id='req', predecessor_run_id='r', expected_revision=0,
                candidate_plan_hash='p2', graph_ref='g1', disposition_policy='drain')
    args.update(kwargs)
    return RevisionService(repository).activate_revision(**args)


def test_same_graph_is_an_in_place_plan_cas(repository):
    pre(repository)
    successor = activate(repository)
    assert successor.run_id == 'r' and successor.active_plan_ref == 'p2'
    assert repository.rows[('runs', 'r')]['body']['active_plan_ref'] == 'p2'


def test_new_graph_creates_a_new_lineage_and_supersedes_the_predecessor(repository):
    pre(repository)
    successor = activate(repository, graph_ref='g2', disposition_policy='cancel')
    assert successor.run_id != 'r' and successor.lineage_id == 'l' and successor.graph_ref == 'g2'
    assert repository.rows[('runs', 'r')]['body']['successor_run_id'] == successor.run_id
    assert repository.rows[('dispatch_gates', 'r')]['body']['open'] is False
    assert repository.rows[('dispatch_gates', successor.run_id)]['body']['open'] is True


def test_replay_is_idempotent_and_stale_revision_is_rejected(repository):
    pre(repository)
    first = activate(repository)
    assert activate(repository).run_id == first.run_id                 # same request_id -> same result
    with pytest.raises(Conflict):
        activate(repository, request_id='req2')                        # predecessor now at revision 1
