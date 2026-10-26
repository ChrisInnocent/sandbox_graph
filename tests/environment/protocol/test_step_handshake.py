import hashlib
import json
import pytest
from sandbox_graph.environment.handshake import RecipeHandshake
from sandbox_graph.environment.permits import ExecutionGrantIssuer,PermitVerifier,GrantLedger
from tests.environment.unit.test_recipe_executor import executor
from sandbox_graph.environment.products.common import file_step


def setup(tmp_path):
    src=tmp_path/'source';src.write_bytes(b'new');dst=tmp_path/'target'
    step=file_step({'blob':{'path':str(src),'sha256':hashlib.sha256(b'new').hexdigest()}},'blob',str(dst))
    runner=executor(tmp_path,(step,));clock=[1000.]
    issuer=ExecutionGrantIssuer(b'x'*32,clock=lambda:clock[0])
    verifier=PermitVerifier(issuer,GrantLedger(tmp_path/'grant.json',clock=lambda:clock[0]),machine_id='host')
    session=RecipeHandshake(runner,verifier,machine_id='host',boot_id='boot',wall=lambda:clock[0],monotonic=lambda:clock[0])
    data={'operation_id':'op','recipe':'test','recipe_hash':runner.recipe_hash('test'),
        'step_id':step.step_id,'expected_before':{str(dst):None}}
    return session,issuer,data,dst,clock


def grant(issuer,challenge,data):
    return issuer.issue(operation_id='op',step_id=data['step_id'],run_id='run',thread_id='thread',owner_epoch=1,
        machine_id='host',resource_generations={'host:host':1},action='product.recipe.step.v1',
        input_data=data,nonce=challenge['nonce'],ttl_seconds=10).model_dump(mode='json')


def test_remote_nonce_is_bound_to_current_locked_session(tmp_path):
    session,issuer,data,dst,clock=setup(tmp_path)
    with session.prepare(data) as prepared:
        challenge=prepared.challenge
        assert not dst.exists()
        result=prepared.execute(grant(issuer,challenge,data))
        assert result['state']=='completed' and dst.read_bytes()==b'new'
        with pytest.raises(PermissionError):prepared.execute(grant(issuer,challenge,data))
    with session.prepare(data) as replay:
        assert replay.observation['state']=='completed' and replay.challenge is None


@pytest.mark.parametrize('failure',['expired','nonce','input','locked'])
def test_delayed_or_forged_grant_and_concurrent_step_cannot_write(tmp_path,failure):
    session,issuer,data,dst,clock=setup(tmp_path)
    with session.prepare(data) as prepared:
        value=grant(issuer,prepared.challenge,data)
        if failure=='expired':clock[0]+=11
        if failure=='nonce':value['nonce']='z'*43
        if failure=='input':value['input_hash']='0'*64
        if failure=='locked':
            with pytest.raises(BlockingIOError):
                with session.prepare(data):pass
        else:
            with pytest.raises(PermissionError):prepared.execute(value)
        assert not dst.exists()


def test_original_started_operation_remains_unknown_and_never_restarts(tmp_path):
    session,issuer,data,dst,clock=setup(tmp_path)
    session.executor.journal.accept(operation_id='op',action='product.recipe.step.v1',input_data=data)
    session.executor.journal.transition('op',state='started',process_identity={'pid':999999,'start_ticks':1})
    with session.prepare(data) as prepared:
        assert prepared.observation['state']=='started' and prepared.challenge is None
    assert not dst.exists()


def test_tenant_subcalls_share_original_grant_deadline(tmp_path,monkeypatch):
    from sandbox_graph.environment.products.common import RecipeStep
    from sandbox_graph.environment.products.server_auth import private_asset,kuasar_pair
    session,issuer,data,_,clock=setup(tmp_path)
    asset=private_asset(tmp_path/'tenant',json.dumps(kuasar_pair('01'*32)).encode())
    step=RecipeStep('tenant','kuasar-tenant',(),asset=asset,target='/private/admin.sock',timeout_s=10)
    session.executor.recipes={'test':(step,)}
    data.update(step_id='tenant',recipe_hash=session.executor.recipe_hash('test'),expected_before={})
    def tenant(pair,*,path,authorize,timeout_s):
        assert authorize()
        clock[0]+=11
        assert not authorize()
        return {'status':'failed','reason':'grant_expired_before_add'}
    monkeypatch.setattr('sandbox_graph.environment.products.server_auth.tenant_over_unix_socket',tenant)
    with session.prepare(data) as prepared:
        result=prepared.execute(grant(issuer,prepared.challenge,data))
    assert result['state']=='failed' and result['result']['reason']=='grant_expired_before_add'


def test_two_frame_helper_protocol_holds_lock_until_real_grant_arrives(tmp_path):
    import os,socket,threading
    from sandbox_graph.environment.remote_agent import serve_granted
    session,issuer,data,dst,_=setup(tmp_path)
    left,right=socket.socketpair();errors=[]
    def remote():
        try:
            with right, right.makefile('rb') as source,right.makefile('wb') as sink:serve_granted(session,source,sink)
        except Exception as exc:errors.append(exc)
    thread=threading.Thread(target=remote);thread.start()
    left.settimeout(3)
    with left,left.makefile('rwb',buffering=0) as wire:
        wire.write(json.dumps({'action':'product.recipe.step.v1','input':data}).encode()+b'\n')
        challenge=json.loads(wire.readline())['challenge']
        assert not dst.exists()
        with pytest.raises(BlockingIOError):
            with session.prepare(data):pass
        wire.write(json.dumps({'grant':grant(issuer,challenge,data)}).encode()+b'\n')
        result=json.loads(wire.readline())['observation']
        assert result['state']=='completed' and dst.read_bytes()==b'new'
    thread.join(3);assert not thread.is_alive() and not errors
