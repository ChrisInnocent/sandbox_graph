import hashlib
import subprocess
from pathlib import Path
import pytest
from sandbox_graph.runtime.candidate_workspace import CandidateWorkspace,classify_diff


def repository_at(tmp_path):
    repo=tmp_path/'source';repo.mkdir()
    def git(*args):return subprocess.check_output(['git','-C',str(repo),*args],stderr=subprocess.DEVNULL).decode().strip()
    git('init');git('config','user.email','fixture@example.invalid');git('config','user.name','Fixture')
    from importlib.metadata import version
    (repo/'app.py').write_text('VALUE=1\n');(repo/'requirements.lock').write_text('pytest=='+version('pytest')+'\n')
    git('add','.');git('commit','-m','base')
    return repo,git('rev-parse','HEAD')


def test_candidate_starts_at_exact_commit_and_never_imports_dirty_user_files(tmp_path):
    repo,base=repository_at(tmp_path)
    (repo/'app.py').write_text('USER_DIRTY=1\n');(repo/'private').write_text('user secret')
    workspace=CandidateWorkspace(repo,tmp_path/'candidates')
    candidate=workspace.create('proposal',base,allowed_paths=('app.py',))
    assert (candidate/'app.py').read_text()=='VALUE=1\n'
    assert not (candidate/'private').exists()
    workspace.edit('proposal',[{'path':'app.py','before_sha256':hashlib.sha256(b'VALUE=1\n').hexdigest(),
        'content':'VALUE=2\n'}])
    frozen=workspace.freeze('proposal',revision=1,graph_ref='candidate:1')
    assert frozen['changed_paths']==['app.py'] and frozen['risk']=='R3'
    assert (repo/'app.py').read_text()=='USER_DIRTY=1\n'
    assert workspace.verify(frozen)['tree_hash']==frozen['tree_hash']
    workspace.edit('proposal',[{'path':'app.py','before_sha256':hashlib.sha256(b'VALUE=2\n').hexdigest(),'content':'VALUE=3\n'}])
    second=workspace.freeze('proposal',revision=2,graph_ref='candidate:2')
    assert second['tree_hash']!=frozen['tree_hash']
    assert (Path(frozen['workspace_ref'])/'app.py').read_text()=='VALUE=2\n'


@pytest.mark.parametrize('path',['../source/app.py','/tmp/escaped','.git/config','requirements.lock'])
def test_provider_edits_cannot_escape_or_exceed_allowlist(tmp_path,path):
    repo,base=repository_at(tmp_path);workspace=CandidateWorkspace(repo,tmp_path/'candidates')
    workspace.create('proposal',base,allowed_paths=('app.py',))
    with pytest.raises((ValueError,PermissionError)):
        workspace.edit('proposal',[{'path':path,'before_sha256':None,'content':'changed'}])
    assert (repo/'app.py').read_text()=='VALUE=1\n'


def test_freeze_rejects_unregistered_or_symlink_mutations_and_tampering(tmp_path):
    repo,base=repository_at(tmp_path);workspace=CandidateWorkspace(repo,tmp_path/'candidates')
    candidate=workspace.create('proposal',base,allowed_paths=('app.py',))
    (candidate/'evil').symlink_to(repo/'app.py')
    with pytest.raises(ValueError):workspace.freeze('proposal',revision=1,graph_ref='g:2')
    (candidate/'evil').unlink();(candidate/'requirements.lock').write_text('unregistered')
    with pytest.raises(PermissionError):workspace.freeze('proposal',revision=1,graph_ref='g:2')


def test_test_or_gate_edits_are_sensitive_even_if_model_claims_documentation():
    assert classify_diff(['docs/usage.md'])=='R0'


def test_initial_workspace_recovers_only_its_matching_partial_staging(tmp_path):
    import json
    repo,base=repository_at(tmp_path);workspace=CandidateWorkspace(repo,tmp_path/'candidates')
    folder=workspace._folder('proposal');staging=folder.with_name('.'+folder.name+'.initializing')
    staging.mkdir();(staging/'binding.json').write_text(json.dumps({'proposal_id':'proposal','base_commit':base,'allowed_paths':['app.py']}))
    (staging/'partial').write_text('interrupted extraction')
    path=workspace.create('proposal',base,allowed_paths=('app.py',))
    assert not staging.exists() and not (path/'partial').exists()
    assert (path/'app.py').read_text()=='VALUE=1\n'


def test_initial_workspace_never_erases_a_conflicting_staging_identity(tmp_path):
    import json
    repo,base=repository_at(tmp_path);workspace=CandidateWorkspace(repo,tmp_path/'candidates')
    folder=workspace._folder('proposal');staging=folder.with_name('.'+folder.name+'.initializing')
    staging.mkdir();(staging/'binding.json').write_text(json.dumps({'proposal_id':'foreign'}))
    with pytest.raises(ValueError):workspace.create('proposal',base,allowed_paths=('app.py',))
    assert (staging/'binding.json').exists() and not folder.exists()
    for paths in (['tests/test_fence.py'],['config/gates/suite.json'],['openspec/config.yaml'],['app.py']):
        assert classify_diff(paths)=='R3'


def test_candidate_root_cannot_be_source_repository_itself(tmp_path):
    repo,_=repository_at(tmp_path)
    with pytest.raises(ValueError):CandidateWorkspace(repo,repo)


def test_git_export_enforces_budget_before_allocating_or_publishing_candidate(tmp_path,monkeypatch):
    import sandbox_graph.runtime.candidate_workspace as module
    repo,base=repository_at(tmp_path);workspace=CandidateWorkspace(repo,tmp_path/'candidates')
    monkeypatch.setattr(module,'MAX_ARCHIVE_BYTES',1024,raising=False)
    with pytest.raises(ValueError,match='archive.*budget'):
        workspace.create('p',base,allowed_paths=('app.py',))
    assert not workspace._folder('p').exists()
    assert (repo/'app.py').read_text()=='VALUE=1\n'


def test_freeze_copy_interruption_retries_same_revision_without_partial_publication(tmp_path,monkeypatch):
    import sandbox_graph.runtime.candidate_workspace as module
    repo,base=repository_at(tmp_path);workspace=CandidateWorkspace(repo,tmp_path/'candidates')
    work=workspace.create('p',base,allowed_paths=('app.py',));(work/'app.py').write_text('VALUE=2\n')
    original=module.shutil.copytree
    def interrupted(src,dst,*args,**kwargs):
        Path(dst).mkdir();(Path(dst)/'app.py').write_text('partial');raise OSError('lost process')
    monkeypatch.setattr(module.shutil,'copytree',interrupted)
    with pytest.raises(OSError):workspace.freeze('p',revision=1,graph_ref='g:2')
    assert not (workspace._folder('p')/'frozen/1').exists()
    monkeypatch.setattr(module.shutil,'copytree',original)
    frozen=workspace.freeze('p',revision=1,graph_ref='g:2')
    assert workspace.freeze('p',revision=1,graph_ref='g:2')==frozen
    with pytest.raises(ValueError):workspace.freeze('p',revision=1,graph_ref='g:different')


def test_freeze_recovers_publication_before_metadata_commit(tmp_path,monkeypatch):
    import sandbox_graph.runtime.candidate_workspace as module
    repo,base=repository_at(tmp_path);workspace=CandidateWorkspace(repo,tmp_path/'candidates')
    work=workspace.create('p',base,allowed_paths=('app.py',));(work/'app.py').write_text('VALUE=2\n')
    original=module._write
    def crash(path,value):
        if path.name=='metadata.json':raise OSError('metadata crash')
        return original(path,value)
    monkeypatch.setattr(module,'_write',crash)
    with pytest.raises(OSError):workspace.freeze('p',revision=1,graph_ref='g:2')
    monkeypatch.setattr(module,'_write',original)
    frozen=workspace.freeze('p',revision=1,graph_ref='g:2')
    assert workspace.verify(frozen)==frozen


def test_partial_original_edit_continues_only_before_or_after_hashes_under_lock(tmp_path):
    repo,base=repository_at(tmp_path);workspace=CandidateWorkspace(repo,tmp_path/'candidates')
    root=workspace.create('p',base,allowed_paths=('app.py','new.py'))
    edits=[{'path':'app.py','before_sha256':hashlib.sha256(b'VALUE=1\n').hexdigest(),'content':'VALUE=2\n'},
           {'path':'new.py','before_sha256':None,'content':'NEW=1\n'}]
    (root/'app.py').write_text('VALUE=2\n')  # original first atomic replacement survived
    workspace.resume_edits('p',edits)
    assert (root/'app.py').read_text()=='VALUE=2\n' and (root/'new.py').read_text()=='NEW=1\n'
    workspace.resume_edits('p',edits)
    (root/'new.py').write_text('EXTERNAL=1\n')
    with pytest.raises(ValueError,match='digest'):workspace.resume_edits('p',edits)
    assert (root/'new.py').read_text()=='EXTERNAL=1\n'
