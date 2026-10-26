from pathlib import Path
import pytest
from sandbox_graph.runtime.candidate_isolation import BwrapBackend


def backend(tmp_path):
    root=tmp_path/'rootfs';root.mkdir();(root/'manifest').write_text('trusted runtime')
    workspace=tmp_path/'candidate';workspace.mkdir()
    return BwrapBackend(rootfs=root,rootfs_hash=BwrapBackend.hash_rootfs(root),
        commands={'syntax':('/usr/bin/python3','-m','compileall','-q','/workspace')},
        state_dir=tmp_path/'state'),workspace


def test_isolation_argv_exposes_only_rootfs_and_candidate_and_clears_environment(tmp_path):
    instance,work=backend(tmp_path)
    argv=instance.argv(work,'syntax')
    assert argv[1:4]==['--unshare-all','--new-session','--die-with-parent']
    assert '--clearenv' in argv and '--cap-drop' in argv
    assert [str(instance.rootfs),'/'] == argv[argv.index('--ro-bind')+1:argv.index('--ro-bind')+3]
    assert [str(work),'/workspace'] == argv[argv.index('--bind')+1:argv.index('--bind')+3]
    assert str(Path.home()) not in argv
    with pytest.raises(ValueError):instance.argv(work,'arbitrary shell')


def test_rootfs_drift_and_symlink_workspace_are_rejected_before_start(tmp_path):
    instance,work=backend(tmp_path)
    (instance.rootfs/'manifest').write_text('changed')
    with pytest.raises(ValueError):instance.argv(work,'syntax')
    alias=tmp_path/'link';alias.symlink_to(work)
    with pytest.raises(ValueError):instance.argv(alias,'syntax')


@pytest.mark.asyncio
async def test_backend_missing_is_explicit_and_never_reports_verified(tmp_path):
    instance,work=backend(tmp_path)
    instance.executable=str(tmp_path/'missing-bwrap')
    result=await instance.execute('request',work,'syntax',timeout_s=.2)
    assert result['status']=='blocked' and result['backend_verified'] is False


@pytest.mark.asyncio
async def test_existing_started_request_keeps_original_identity_without_resubmission(tmp_path):
    import json
    instance,work=backend(tmp_path)
    identity=instance.identity('request',work,'syntax',b'',timeout_s=1.)
    path=instance.record_path('request')
    path.write_text(json.dumps({'identity':identity,'status':'running','pid':999999,'start_ticks':123}))
    result=await instance.execute('request',work,'syntax',timeout_s=1.)
    assert result['status']=='unknown' and result['pid']==999999


@pytest.mark.parametrize('overlap',['workspace_in_rootfs','state_in_workspace','workspace_in_state'])
def test_candidate_and_control_mounts_cannot_overlap(tmp_path,overlap):
    instance,work=backend(tmp_path)
    if overlap=='workspace_in_rootfs':work=instance.rootfs/'candidate';work.mkdir()
    if overlap=='state_in_workspace':work=tmp_path
    if overlap=='workspace_in_state':work=instance.state_dir/'candidate';work.mkdir()
    with pytest.raises(ValueError):instance.argv(work,'syntax')


@pytest.mark.asyncio
async def test_isolation_setup_failure_has_explicit_capability_evidence(tmp_path,monkeypatch):
    import asyncio
    instance,work=backend(tmp_path)
    # Controlled executable probe, not a verified isolation acceptance.
    class Process:
        returncode=1
        async def communicate(self):return b'',b'bwrap: Unexpected capabilities but not setuid'
    async def spawn(*args,**kwargs):return Process()
    monkeypatch.setattr(asyncio,'create_subprocess_exec',spawn)
    monkeypatch.setattr('shutil.which',lambda _: '/usr/bin/bwrap')
    result=await instance.capabilities()
    assert result['status']=='blocked' and result['reason']=='bubblewrap_setup_failed'
    assert result['backend_verified'] is False and result['stderr_sha256']

@pytest.mark.asyncio
async def test_cancel_observes_owned_process_exit_and_never_restarts_request(tmp_path):
    import asyncio,json,sys
    instance,work=backend(tmp_path)
    process=await asyncio.create_subprocess_exec(sys.executable,'-c','import time; time.sleep(30)',start_new_session=True)
    try:
        row={'identity':instance.identity('request',work,'syntax',b'',timeout_s=1.),'status':'running',
            'pid':process.pid,'start_ticks':instance._pid_identity(process.pid),
            'boot_id':instance._boot_id()}
        instance.record_path('request').write_text(json.dumps(row))
        stopped=await instance.cancel('request',timeout_s=2)
        assert stopped['status']=='cancelled' and stopped['cleanup']=='complete'
        assert (await instance.reconcile('request'))['cleanup']=='complete'
        assert (await instance.execute('request',work,'syntax',timeout_s=1.))['status']=='cancelled'
    finally:
        if process.returncode is None:
            try:process.kill()
            except ProcessLookupError:pass
        await process.wait()

@pytest.mark.asyncio
async def test_unknown_or_reused_process_identity_never_signals_foreign_process(tmp_path,monkeypatch):
    import json
    instance,work=backend(tmp_path)
    row={'identity':instance.identity('request',work,'syntax',b'',timeout_s=1.),'status':'running',
        'pid':42,'start_ticks':1,'boot_id':instance._boot_id()}
    instance.record_path('request').write_text(json.dumps(row))
    monkeypatch.setattr(instance,'_pid_identity',lambda pid:2)
    monkeypatch.setattr('os.killpg',lambda *a:pytest.fail('must not signal reused group'))
    assert (await instance.cancel('request',timeout_s=.1))['status']=='unknown'

@pytest.mark.asyncio
async def test_terminal_exit_requires_process_group_observation_and_keeps_result(tmp_path,monkeypatch):
    import json
    instance,work=backend(tmp_path)
    row={'identity':instance.identity('request',work,'syntax',b'',timeout_s=1.),'status':'completed',
        'returncode':0,'pid':42,'start_ticks':1,'boot_id':instance._boot_id()}
    instance.record_path('request').write_text(json.dumps(row))
    def missing(pid):raise FileNotFoundError()
    monkeypatch.setattr(instance,'_pid_identity',missing)
    monkeypatch.setattr(instance,'_group_members',lambda _:[])
    result=await instance.reconcile('request')
    assert result['status']=='completed' and result['cleanup']=='complete' and result['returncode']==0


def test_late_execution_cannot_overwrite_cancellation_closure(tmp_path):
    instance,work=backend(tmp_path)
    identity=instance.identity('request',work,'syntax',b'',timeout_s=1.)
    path=instance.record_path('request')
    instance._store(path,{'identity':identity,'status':'cancelled','cleanup':'complete'})
    result=instance._store(path,{'identity':identity,'status':'completed','returncode':0})
    assert result['status']=='cancelled' and result['cleanup']=='complete'


@pytest.mark.asyncio
async def test_authority_is_rechecked_after_rootfs_validation_before_spawn(tmp_path,monkeypatch):
    import asyncio
    instance,work=backend(tmp_path);validated=[]
    monkeypatch.setattr('shutil.which',lambda _: '/usr/bin/bwrap')
    original=instance.argv
    def argv(*args,**kwargs):
        result=original(*args,**kwargs);validated.append(True);return result
    monkeypatch.setattr(instance,'argv',argv)
    async def forbidden(*args,**kwargs):pytest.fail('expired authority must never spawn')
    monkeypatch.setattr(asyncio,'create_subprocess_exec',forbidden)
    def guard():
        assert validated
        raise PermissionError('revoked during rootfs validation')
    result=await instance.execute('request',work,'syntax',timeout_s=1,before_start=guard)
    assert result['status']=='blocked' and result['submission']=='proven_not_started'
    assert (await instance.reconcile('request'))['cleanup']=='complete'
