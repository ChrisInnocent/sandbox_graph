import hashlib
import io
import tarfile
import pytest
from sandbox_graph.environment.products.common import file_step,archive_step,command
from sandbox_graph.environment.products.executor import RecipeExecutor
from sandbox_graph.environment.journal import OperationJournal


def executor(tmp_path,steps,authorize=lambda *a:True):
    journal=OperationJournal(tmp_path/'state/journal.json',machine_id='m',boot_id='b')
    return RecipeExecutor({'test':steps},journal=journal,lock_dir=tmp_path/'locks',
        backup_dir=tmp_path/'backup',authorize=authorize)


def test_file_install_is_cas_verified_and_idempotent(tmp_path):
    src=tmp_path/'source';src.write_bytes(b'binary')
    target=tmp_path/'destination'
    step=file_step({'binary':{'path':str(src),'sha256':hashlib.sha256(src.read_bytes()).hexdigest()}},'binary',str(target))
    service=executor(tmp_path,(step,))
    receipt=service.execute('operation-1','test',expected_before={str(target):None})
    assert receipt['status']=='completed' and target.read_bytes()==b'binary'
    assert target.stat().st_mode & 0o777 == 0o755
    assert service.execute('operation-1','test',expected_before={str(target):None})==receipt
    with pytest.raises(RuntimeError):service.execute('operation-1','test',expected_before={str(target):'a'*64})


def test_unauthorized_or_modified_asset_never_writes_target(tmp_path):
    src=tmp_path/'source';src.write_bytes(b'wrong')
    target=tmp_path/'destination'
    step=file_step({'binary':{'path':str(src),'sha256':'a'*64}},'binary',str(target))
    service=executor(tmp_path,(step,),authorize=lambda *a:False)
    with pytest.raises(PermissionError):service.execute('denied','test',expected_before={str(target):None})
    service.authorize=lambda *a:True
    assert service.execute('digest-error','test',expected_before={str(target):None})['status']=='failed'
    assert not target.exists()


def test_archive_cannot_escape_or_install_symlinks(tmp_path):
    archive=tmp_path/'input.tar'
    with tarfile.open(archive,'w') as tar:
        entry=tarfile.TarInfo('../escape');entry.size=1;tar.addfile(entry,io.BytesIO(b'x'))
    target=tmp_path/'installed'
    step=archive_step({'package':{'path':str(archive),'sha256':hashlib.sha256(archive.read_bytes()).hexdigest()}},'package',str(target))
    result=executor(tmp_path,(step,)).execute('archive','test',expected_before={str(target):None})
    assert result['status']=='failed' and not target.exists() and not (tmp_path/'escape').exists()


def test_started_operation_is_not_blindly_reexecuted_after_restart(tmp_path):
    service=executor(tmp_path,(command('safe','true'),))
    payload={'recipe':'test','recipe_hash':service.recipe_hash('test'),'expected_before':{}}
    service.journal.accept(operation_id='old',action='product.recipe.v1',input_data=payload)
    service.journal.transition('old',state='started',process_identity={'pid':99999,'start_ticks':1})
    result=service.execute('old','test',expected_before={})
    assert result['status']=='unknown' and result['reason']=='original_execution_requires_reconciliation'


def test_cube_sourced_configuration_is_validated_before_any_command(tmp_path,monkeypatch):
    config=tmp_path/'cube.env';config.write_text('API_KEY=$(touch /unwanted)\n')
    service=executor(tmp_path,(command('cube:install','bash','/locked/install.sh',
        environment={'ONE_CLICK_ENV_FILE':str(config)}),))
    def forbidden(*args,**kwargs):raise AssertionError('must not spawn with executable config')
    monkeypatch.setattr('subprocess.Popen',forbidden)
    result=service.execute('bad-config','test',expected_before={})
    assert result['status']=='failed' and result['reason']=='ValueError'


def test_each_completed_step_has_durable_receipt_before_next_step(tmp_path):
    src=tmp_path/'source';src.write_bytes(b'locked')
    asset={'blob':{'path':str(src),'sha256':hashlib.sha256(src.read_bytes()).hexdigest()}}
    targets=[tmp_path/'first',tmp_path/'second']
    steps=tuple(file_step(asset,'blob',str(target)) for target in targets)
    # Unique recipe step IDs are required to reconcile partial installation.
    from dataclasses import replace
    steps=(replace(steps[0],step_id='file:first'),replace(steps[1],step_id='file:second'))
    service=executor(tmp_path,steps)
    result=service.execute('install','test',expected_before={str(p):None for p in targets})
    assert result['status']=='completed'
    for row in result['steps']:
        receipt=service.journal.get(row['operation_id'])
        assert receipt['state']=='completed' and receipt['result']['file']['after_sha256']==hashlib.sha256(b'locked').hexdigest()


def test_command_keeps_private_digest_bound_logs_and_owned_process_identity(tmp_path):
    import sys
    from pathlib import Path
    service=executor(tmp_path,(command('print',sys.executable,'-c','print("installed")'),))
    result=service.execute('with-log','test',expected_before={})
    assert result['status']=='completed'
    step=result['steps'][0]
    assert Path(step['logs'][0]['private_path']).read_bytes()==b'installed\n'
    assert Path(step['logs'][0]['private_path']).stat().st_mode&0o777==0o600
    assert service.journal.get(step['operation_id'])['execution']['pid']>0


def test_cancel_stops_exact_journaled_command_and_retains_original_logs(tmp_path):
    import sys,time,threading
    from sandbox_graph.environment.owned_execution import cancel_original
    service=executor(tmp_path,(command('wait',sys.executable,'-c','import time;time.sleep(30)'),))
    answer=[]
    thread=threading.Thread(target=lambda:answer.append(service.execute('original','test',expected_before={})))
    thread.start()
    from sandbox_graph.domain.models import canonical_hash
    identity='recipe-step:'+canonical_hash({'operation_id':'original','step_id':'wait'})
    try:
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            row=service.journal.get(identity)
            if row and row.get('execution'):break
            time.sleep(.01)
        assert row.get('execution')
        result=cancel_original(service.journal,operation_id=identity,input_hash=row['input_hash'],lock_dir=service.lock_dir)
        thread.join(timeout=5)
        assert not thread.is_alive() and result['state']=='cancelled'
        assert answer[0]['steps'][0]['logs'][0]['private_path']
    finally:
        if thread.is_alive():
            import os,signal
            row=service.journal.get(identity)
            if row and row.get('execution'):os.killpg(row['execution']['pid'],signal.SIGKILL)
            thread.join(timeout=5)
