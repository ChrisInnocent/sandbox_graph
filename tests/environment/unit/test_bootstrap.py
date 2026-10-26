"""EAT-36: real local files/processes; wheel fixture is explicitly synthetic."""
import hashlib
import io
import json
import sys
import zipfile
from pathlib import Path

import pytest


def fixture_files(tmp_path):
    wheel=tmp_path/'sandbox_graph-0.0.1-py3-none-any.whl'
    with zipfile.ZipFile(wheel,'w') as archive:
        files={
            'sandbox_graph/__init__.py':'', 'sandbox_graph/environment/__init__.py':'',
            'sandbox_graph/environment/remote_agent.py':
                'import json, pathlib\ndef bootstrap_check(root):\n'
                '    assert len((pathlib.Path(root)/"execution-grant.key").read_bytes()) >= 32\n'
                '    assert json.loads((pathlib.Path(root)/"recipes.json").read_text())["schema_version"] == 1\n'
                '    return {"protocols":["stdio-json","stdio-grant-v1"]}\n'
                'def main(*, configuration_root):\n    print(json.dumps(bootstrap_check(configuration_root)))\n    return 0\n',
            'sandbox_graph-0.0.1.dist-info/METADATA':'Metadata-Version: 2.1\nName: sandbox-graph\nVersion: 0.0.1\n',
            'sandbox_graph-0.0.1.dist-info/WHEEL':'Wheel-Version: 1.0\nGenerator: synthetic-test\nRoot-Is-Purelib: true\nTag: py3-none-any\n',
        }
        for name,body in files.items():archive.writestr(name,body)
        archive.writestr('sandbox_graph-0.0.1.dist-info/RECORD',''.join(f'{name},,\n' for name in files))
    key=tmp_path/'key';key.write_bytes(b'PRIVATE_BOOTSTRAP_SENTINEL'+b'x'*32);key.chmod(0o600)
    catalog=tmp_path/'catalog';catalog.write_text('{"schema_version":1,"recipes":{}}')
    asset=tmp_path/'binary';asset.write_bytes(b'fixed-binary')
    return [(wheel,'wheel'),(key,'grant_key'),(catalog,'recipes'),(asset,'asset')]


def inputs(tmp_path):
    from sandbox_graph.environment.bootstrap import build_bundle, stream_bundle
    bundle=tmp_path/'bundle'
    info=build_bundle(fixture_files(tmp_path),bundle)
    request={'schema_version':1,'operation_id':'bootstrap/one','machine_id':'machine-a',
             'root':str(tmp_path/'remote'),'expected_launcher_sha256':None,
             'bundle_sha256':info['bundle_sha256'],'manifest':info['manifest']}
    machine=tmp_path/'machine-id';machine.write_text('machine-a')
    def stream():return io.BytesIO(b''.join(stream_bundle(bundle,request)))
    return request,stream,machine


def test_actual_offline_bootstrap_reuses_original_release_and_runs_entry(tmp_path,monkeypatch):
    from sandbox_graph.environment import bootstrap_remote as remote
    receive=remote.receive
    import subprocess
    request,stream,machine=inputs(tmp_path)
    write=remote.atomic
    def cut_after_activation(path,raw,mode=0o600):
        if path.parent.name=='requests' and 'result' in json.loads(raw):raise OSError('receipt publication cut')
        return write(path,raw,mode)
    monkeypatch.setattr(remote,'atomic',cut_after_activation)
    with pytest.raises(OSError,match='receipt publication cut'):
        receive(stream(),machine_id_file=machine,home=tmp_path/'home')
    assert (Path(request['root'])/'bin/sandbox-graph-remote-agent').is_file()
    monkeypatch.setattr(remote,'atomic',write)
    result=receive(stream(),machine_id_file=machine,home=tmp_path/'home')
    assert result['status']=='completed'
    assert 'PRIVATE_BOOTSTRAP_SENTINEL' not in json.dumps(result)
    entry=Path(result['helper_path'])
    assert entry.is_file() and entry.stat().st_mode&0o777==0o700
    run=subprocess.run([str(entry),'--stdio-json'],capture_output=True,text=True,timeout=15)
    assert run.returncode==0 and json.loads(run.stdout)['protocols']==['stdio-json','stdio-grant-v1']
    assert receive(stream(),machine_id_file=machine,home=tmp_path/'home')==result
    requests=list((Path(request['root'])/'requests').glob('*.json'))
    assert len(requests)==1 and json.loads(requests[0].read_text())['attempts']==1
    blob=Path(result['assets']['binary']);assert blob.read_bytes()==b'fixed-binary'
    assert blob.stat().st_mode&0o777==0o600
    # A completed request cannot silently recreate an externally removed entry.
    original=entry.read_bytes();entry.unlink()
    with pytest.raises(ValueError,match='launcher'):
        receive(stream(),machine_id_file=machine,home=tmp_path/'home')
    entry.write_bytes(original);entry.chmod(0o700)
    entry.write_text('foreign modification')
    with pytest.raises(ValueError,match='launcher'):
        receive(stream(),machine_id_file=machine,home=tmp_path/'home')


@pytest.mark.parametrize('fault',['machine','corrupt','truncated','traversal'])
def test_bootstrap_rejects_untrusted_input_without_activation(tmp_path,fault):
    from sandbox_graph.environment.bootstrap_remote import receive
    request,stream,machine=inputs(tmp_path)
    if fault=='machine':machine.write_text('other')
    raw=stream().getvalue()
    if fault=='corrupt':raw=raw[:-1]+bytes([raw[-1]^1])
    if fault=='truncated':raw=raw[:-3]
    if fault=='traversal':
        request['root']=str(tmp_path/'x'/'..'/'escape')
        raw=json.dumps(request).encode()+b'\n'+raw.split(b'\n',1)[1]
    with pytest.raises((ValueError,EOFError)):
        receive(io.BytesIO(raw),machine_id_file=machine,home=tmp_path/'home')
    assert not (Path(request['root'])/'bin/sandbox-graph-remote-agent').exists()


def test_real_helper_self_check_loads_private_grant_key_and_recipe_catalog(tmp_path):
    from sandbox_graph.environment.remote_agent import bootstrap_check,RemoteAgentError
    (tmp_path/'execution-grant.key').write_bytes(b'k'*32)
    (tmp_path/'execution-grant.key').chmod(0o600)
    (tmp_path/'recipes.json').write_text('{"schema_version":1,"recipes":{}}')
    (tmp_path/'recipes.json').chmod(0o600)
    assert bootstrap_check(tmp_path)=={'protocols':['stdio-json','stdio-grant-v1']}
    (tmp_path/'execution-grant.key').chmod(0o644)
    with pytest.raises(RemoteAgentError):bootstrap_check(tmp_path)


def test_unknown_build_reentry_is_bounded_and_grant_rotation_rejected(tmp_path,monkeypatch):
    from sandbox_graph.environment import bootstrap_remote as remote
    request,stream,machine=inputs(tmp_path)
    calls=[]
    def interrupted(*args,**kwargs):calls.append(args[0]);raise OSError('interrupted')
    monkeypatch.setattr(remote,'command',interrupted)
    for _ in range(3):
        with pytest.raises(OSError):remote.receive(stream(),machine_id_file=machine,home=tmp_path/'home')
    with pytest.raises(ValueError,match='budget'):
        remote.receive(stream(),machine_id_file=machine,home=tmp_path/'home')
    assert len(calls)==3
    assert not (Path(request['root'])/'bin/sandbox-graph-remote-agent').exists()
    changed=dict(request,operation_id='new-operation')
    trust=Path(request['root'])/'grant-key.sha256';trust.write_text('a'*64);trust.chmod(0o600)
    raw=remote.encode(changed)+b'\n'+stream().getvalue().split(b'\n',1)[1]
    with pytest.raises(ValueError,match='rotation'):
        remote.receive(io.BytesIO(raw),machine_id_file=machine,home=tmp_path/'home')


def test_host_execution_lock_prevents_bootstrap_before_any_build(tmp_path):
    import fcntl
    from sandbox_graph.environment import bootstrap_remote as remote
    request,stream,machine=inputs(tmp_path)
    lock=remote.private_dir(tmp_path/'home/.local/state/sandbox-graph/locks')/'host.lock'
    with lock.open('w') as held:
        fcntl.flock(held,fcntl.LOCK_EX|fcntl.LOCK_NB)
        with pytest.raises(BlockingIOError):remote.receive(stream(),machine_id_file=machine,home=tmp_path/'home')
    assert not (Path(request['root'])/'requests').exists()


@pytest.mark.asyncio
async def test_transport_runs_fixed_receiver_with_streamed_assets(tmp_path):
    from sandbox_graph.environment.bootstrap import deploy
    request,stream,machine=inputs(tmp_path)
    known=tmp_path/'known';known.touch()
    identity=tmp_path/'ssh-key';identity.write_text('synthetic-ssh-key');identity.chmod(0o600)
    fake=tmp_path/'ssh-fixture'
    replacement='print(json.dumps(receive(sys.stdin.buffer,machine_id_file='+repr(str(machine))+',home='+repr(str(tmp_path/'home'))+')))'
    fake.write_text('#!'+sys.executable+'\nimport sys,shlex\n'
        'args=shlex.split(sys.argv[-1])\nassert args[:4]==["python3","-I","-B","-c"]\n'
        'assert "PRIVATE_BOOTSTRAP_SENTINEL" not in sys.argv[-1]\n'
        'source=args[-1].rsplit("raise SystemExit(main())",1)[0]\n'
        'exec(source+'+repr(replacement)+')\n')
    fake.chmod(0o700)
    result=await deploy(bundle=tmp_path/'bundle',request=request,address='synthetic.example',port=22,
        user='fixture',known_hosts=known,identity_file=identity,executable=str(fake),timeout=120)
    assert result['status']=='completed'
    assert Path(result['assets']['binary']).read_bytes()==b'fixed-binary'


def test_bootstrap_command_has_a_private_bounded_log_and_deadline(tmp_path,monkeypatch):
    from sandbox_graph.environment import bootstrap_remote as remote
    monkeypatch.setattr(remote,'MAX_LOG',1024)
    with pytest.raises(ValueError,match='log byte limit'):
        remote.command([sys.executable,'-c','print("x"*4096)'],tmp_path/'large.log',[],timeout=5)
    assert (tmp_path/'large.log').stat().st_size<=1024
    assert (tmp_path/'large.log').stat().st_mode&0o777==0o600
    with pytest.raises(TimeoutError):
        remote.command([sys.executable,'-c','import time;time.sleep(10)'],tmp_path/'timeout.log',[],timeout=.1)
    with pytest.raises(remote.CommandFailed) as failure:
        remote.command([sys.executable,'-c','raise SystemExit(7)'],tmp_path/'failed.log',[],timeout=5)
    assert failure.value.returncode==7


def test_bootstrap_child_retains_host_lock_after_controller_hardkill(tmp_path):
    import fcntl,os,signal,subprocess,time
    lock=tmp_path/'host.lock';marker=tmp_path/'child.pid'
    script='''
import os,sys,fcntl
from pathlib import Path
from sandbox_graph.environment.bootstrap_remote import command
root=Path(sys.argv[1]);fd=os.open(root/'host.lock',os.O_CREAT|os.O_RDWR,0o600)
fcntl.flock(fd,fcntl.LOCK_EX)
code="import os,time;from pathlib import Path;Path("+repr(str(root/'child.pid'))+").write_text(str(os.getpid()));time.sleep(30)"
command([sys.executable,'-c',code],root/'child.log',[fd],timeout=40)
'''
    parent=subprocess.Popen([sys.executable,'-c',script,str(tmp_path)])
    child=None
    try:
        deadline=time.monotonic()+10
        while not marker.exists() and time.monotonic()<deadline:time.sleep(.01)
        assert marker.exists(),'child did not start'
        child=int(marker.read_text());parent.kill();parent.wait(timeout=5)
        with lock.open('r') as stream:
            with pytest.raises(BlockingIOError):fcntl.flock(stream,fcntl.LOCK_EX|fcntl.LOCK_NB)
        os.killpg(child,signal.SIGKILL);child=None
    finally:
        if child is not None:os.killpg(child,signal.SIGKILL)
        if parent.poll() is None:parent.kill();parent.wait(timeout=5)


def test_bootstrap_cli_build_and_private_key_mode(tmp_path,capsys):
    from scripts.environment.bootstrap_helper import main
    files=fixture_files(tmp_path)
    config=tmp_path/'build.json'
    config.write_text(json.dumps({'files':[{'path':str(path),'kind':kind} for path,kind in files]}))
    assert main(['build','--input',str(config),'--bundle',str(tmp_path/'bundle')])==0
    output=capsys.readouterr().out
    assert json.loads(output)['status']=='prepared' and 'PRIVATE_BOOTSTRAP_SENTINEL' not in output
    next(path for path,kind in files if kind=='grant_key').chmod(0o644)
    assert main(['build','--input',str(config),'--bundle',str(tmp_path/'bad-bundle')])==2
    assert not (tmp_path/'bad-bundle').exists()


def test_expanded_wheel_budget_is_checked_before_running_installer(tmp_path,monkeypatch):
    from sandbox_graph.environment import bootstrap_remote as remote
    request,stream,machine=inputs(tmp_path)
    monkeypatch.setattr(remote,'MAX_EXPANDED',16)
    monkeypatch.setattr(remote,'command',lambda *a,**k:pytest.fail('installer must not start'))
    with pytest.raises(ValueError,match='expanded wheel budget'):
        remote.receive(stream(),machine_id_file=machine,home=tmp_path/'home')
    assert not (Path(request['root'])/'releases').exists()
