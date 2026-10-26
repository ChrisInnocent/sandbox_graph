from pathlib import Path
import sys
from types import SimpleNamespace
import pytest
from sandbox_graph.services.worker_bundles import WorkerBundleService
from tests.orchestration.unit.test_evolution_service import setup,edits


def test_bundle_copies_exact_frozen_tree_and_rechecks_startup_bytes(repository,tmp_path,monkeypatch):
    service,proposal,target,token,_=setup(repository,tmp_path);service.open(proposal,target=target,token=token)
    candidate=service.edit_revision(proposal.proposal_id,request_id='edit',edits=edits(2),graph_ref='g:2',token=token)
    bundles=WorkerBundleService(repository,root=tmp_path/'bundles')
    ref=bundles.build(candidate,token=token,workspace=service.workspace)
    row=bundles.verify(ref)
    root=Path(row['path'])/'tree'
    assert (root/'app.py').read_text()=='VALUE=2\n'
    with pytest.raises(PermissionError):bundles.register_ready('worker',ref,token=token,modules=('sandbox_graph',),ttl=30)
    monkeypatch.setitem(sys.modules,'candidate_fixture',SimpleNamespace(__file__=str(root/'app.py')))
    bundles.register_ready('worker',ref,token=token,modules=('candidate_fixture',),ttl=30)
    with repository.transaction() as tx:
        assert bundles.ready_in(tx,tx.get('worker_registrations','worker')['body'])
    (root/'app.py').chmod(0o600);(root/'app.py').write_text('VALUE=9\n')
    with pytest.raises(ValueError,match='tree'):bundles.verify(ref)
    with pytest.raises(ValueError):bundles.register_ready('worker',ref,token=token,modules=('candidate_fixture',),ttl=30)


def test_readiness_expires_when_attesting_owner_is_revoked(repository,tmp_path,monkeypatch):
    service,proposal,target,token,_=setup(repository,tmp_path);service.open(proposal,target=target,token=token)
    candidate=service.edit_revision(proposal.proposal_id,request_id='edit',edits=edits(2),graph_ref='g:2',token=token)
    bundles=WorkerBundleService(repository,root=tmp_path/'bundles');ref=bundles.build(candidate,token=token,workspace=service.workspace)
    root=Path(bundles.verify(ref)['path'])/'tree'
    monkeypatch.setitem(sys.modules,'candidate_fixture',SimpleNamespace(__file__=str(root/'app.py')))
    registered=bundles.register_ready('worker',ref,token=token,modules=('candidate_fixture',),ttl=30)
    with repository.transaction() as tx:
        tx.release_thread(token)
        assert not bundles.ready_in(tx,registered)


def test_deployment_binding_allows_multiple_ready_owners_without_changing_runtime(repository):
    body={'candidate_hash':'candidate','runtime_hash':'runtime','bundle_ref':'bundle','worker_id':'first'}
    with repository.transaction() as tx:
        WorkerBundleService.register_deployment_in(tx,'candidate',body)
        WorkerBundleService.register_deployment_in(tx,'candidate',body|{'worker_id':'second'})
        assert len(tx.scan('candidate_deployment_workers'))==2
        assert tx.get('candidate_deployment_bindings','candidate')['body']==body
        with pytest.raises(ValueError):WorkerBundleService.register_deployment_in(tx,'candidate',body|{'runtime_hash':'changed'})


def test_dependency_lock_must_match_the_actual_worker_interpreter(tmp_path):
    from sandbox_graph.services.worker_bundles import dependency_versions
    (tmp_path/'requirements.lock').write_text('pytest==0.0.0\n')
    with pytest.raises(ValueError,match='differs'):dependency_versions(tmp_path)
    (tmp_path/'requirements.lock').write_text('pytest>=1\n')
    with pytest.raises(ValueError,match='exact'):dependency_versions(tmp_path)
