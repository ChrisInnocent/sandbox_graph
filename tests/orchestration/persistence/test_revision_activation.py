"""L2 D14 (task 11.2): concurrent graph-version activation has one winner. Skips without PostgreSQL."""
import pytest
from threading import Barrier, Thread

from sandbox_graph.domain.models import RunRecord
from sandbox_graph.persistence.repository import Conflict
from sandbox_graph.services.revisions import RevisionService


def _activate(repo, request_id, plan, errors, barrier):
    try:
        barrier.wait()
        RevisionService(repo).activate_revision(
            request_id=request_id, predecessor_run_id='r', expected_revision=0,
            candidate_plan_hash=plan, graph_ref='g', disposition_policy='drain')
    except Conflict as exc:
        errors.append(exc)


def test_concurrent_activation_has_exactly_one_winner(postgres):
    repo, _ = postgres
    RevisionService(repo).create(RunRecord(run_id='r', root_thread_id='t', graph_ref='g',
                                           active_plan_ref='p0', contract_ref='c',
                                           auth_ref='a', lineage_id='l'))
    barrier, errors = Barrier(2), []
    threads = [Thread(target=_activate, args=(repo, f'req{i}', f'p{i}', errors, barrier))
               for i in (1, 2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert len(errors) == 1
    with repo.transaction() as tx:
        assert tx.get('runs', 'r')['body']['active_plan_ref'] in ('p1', 'p2')


def test_request_replay_is_idempotent_and_stale_revision_rejected(postgres):
    repo, _ = postgres
    RevisionService(repo).create(RunRecord(run_id='r', root_thread_id='t', graph_ref='g',
                                           active_plan_ref='p0', contract_ref='c',
                                           auth_ref='a', lineage_id='l'))
    service = RevisionService(repo)
    first = service.activate_revision(request_id='req', predecessor_run_id='r', expected_revision=0,
                                      candidate_plan_hash='p1', graph_ref='g', disposition_policy='drain')
    assert service.activate_revision(request_id='req', predecessor_run_id='r', expected_revision=0,
                                     candidate_plan_hash='p1', graph_ref='g',
                                     disposition_policy='drain').run_id == first.run_id
    with pytest.raises(Conflict):
        service.activate_revision(request_id='req2', predecessor_run_id='r', expected_revision=0,
                                  candidate_plan_hash='p2', graph_ref='g', disposition_policy='drain')
