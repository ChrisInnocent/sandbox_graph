"""D18.5 / ORC-040,049,050,058: closure classification, CAS and partial archive."""
import pytest

from sandbox_graph.domain.models import NodeResult
from sandbox_graph.persistence.repository import Conflict
from sandbox_graph.services.closure import ClosureService


def node(*, disposition='fulfilled', data='valid', cleanup='complete', execution='succeeded'):
    return NodeResult(execution_status=execution, scope_disposition=disposition,
                      data_status=data, cleanup_status=cleanup)


def seal(repository, **overrides):
    args = dict(run_id='r', expected_revision=0, analysis_ref='a', report_analysis_ref='a',
                excel_analysis_ref='a', scopes={}, inflight=False, artifact_refs=())
    args.update(overrides)
    return ClosureService(repository).seal_archive(**args)


def test_classify_scope_open_partial_complete(repository):
    assert ClosureService.classify_scope(node(disposition=None)) == 'open'
    assert ClosureService.classify_scope(node(disposition=None), 'p') == 'open'
    assert ClosureService.classify_scope(node(data='insufficient'), 'p') == 'partial'
    assert ClosureService.classify_scope(node(cleanup='unknown'), 'p') == 'partial'
    assert ClosureService.classify_scope(node(), 'p') == 'complete'


def test_seal_archive_rejects_inflight_mismatched_analysis_and_open_scope(repository):
    with pytest.raises(Conflict):
        seal(repository, inflight=True)
    with pytest.raises(Conflict):
        seal(repository, report_analysis_ref='b')
    with pytest.raises(Conflict):
        seal(repository, scopes={'w': (node(disposition=None), 'p')})


def test_seal_archive_rejects_a_stale_closure_revision(repository):
    repository.rows[('closure_revisions', 'r')] = {'key': 'r', 'revision': 0,
                                                   'body': {'plan_ref': 'p', 'revision': 1}}
    with pytest.raises(Conflict):
        seal(repository, expected_revision=0)


def test_partial_scope_produces_a_partial_archive(repository):
    archive = seal(repository, scopes={'w': (node(data='insufficient'), 'p')})
    assert archive['status'] == 'partial'
    assert archive['scope_status'] == {'w': 'partial'}
    assert archive['analysis_ref'] == 'a'
