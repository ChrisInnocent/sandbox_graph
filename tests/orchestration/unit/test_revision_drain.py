"""P11.4 / ORC-056e,f: drain only affects in-flight jobs with prior permits."""
import pytest

from sandbox_graph.domain.models import RunRecord
from sandbox_graph.services.revisions import RevisionService


def pre(repository):
    RevisionService(repository).create(RunRecord(
        run_id='r', root_thread_id='t', graph_ref='g1', active_plan_ref='p1',
        contract_ref='c', auth_ref='a', lineage_id='l'))


def job(**extra):
    data = dict(job_id='j1', job_kind='work', scope_ref='w', thread_id='t', activation_id='a',
                dedupe_key='d', due_at=0.0, deadline=10 ** 9, run_id='r', status='pending')
    data.update(extra)
    return data


def test_drain_supersedes_new_jobs_but_keeps_inflight_with_permits(repository):
    pre(repository)
    with repository.transaction() as tx:
        tx.put('jobs', 'inflight', job(job_id='inflight', scope_ref='g'))
        tx.put('jobs', 'fresh', job(job_id='fresh', scope_ref='h'))
        RevisionService.permit_in(tx, 'r', 'op-inflight', generation=1, scope='g')
    RevisionService(repository).activate_revision(
        request_id='req', predecessor_run_id='r', expected_revision=0, candidate_plan_hash='p2',
        graph_ref='g2', disposition_policy='drain')
    assert repository.rows[('jobs', 'inflight')]['body']['status'] == 'draining'
    assert repository.rows[('jobs', 'fresh')]['body']['status'] == 'superseded'
