import json
from types import SimpleNamespace
import pytest
from sandbox_bench.adapters import Adapter


def config(tmp_path):
    return {'product':'agentenv','api_url':'http://127.0.0.1:8080','data_url':'http://127.0.0.1:8081',
        'source':{'kind':'template','id':'template'},
        'instance_credentials':{'path':str(tmp_path/'private'),'scope':'run','generation':1}}


def response(body):return SimpleNamespace(body=json.dumps(body).encode(),headers_s=.1,complete_s=.2)


def test_adapter_restores_private_token_after_restart_without_executing_first_command(tmp_path,monkeypatch):
    original=Adapter(config(tmp_path))
    monkeypatch.setattr(original,'_control',lambda *args,**kwargs:response({'sandboxID':'s','envdAccessToken':'data-secret','state':'running'}))
    original.create('run',2)
    restored=Adapter(config(tmp_path));calls=[]
    def http(method,url,**kwargs):
        calls.append((method,url,kwargs))
        if method=='GET':return response({'state':'running'})
        raise RuntimeError('controlled data-plane boundary')
    monkeypatch.setattr(restored,'_http',http)
    assert restored.inspect({'sandbox_id':'s'},2)['state']=='running'
    assert len(calls)==1 and calls[0][0]=='GET'
    with pytest.raises(RuntimeError,match='controlled'):
        restored.execute({'sandbox_id':'s'},'true',2)
    assert calls[-1][2]['headers']['X-Access-Token']=='data-secret'


def test_wrong_run_is_rejected_before_remote_creation(tmp_path,monkeypatch):
    adapter=Adapter(config(tmp_path));calls=[]
    monkeypatch.setattr(adapter,'_control',lambda *a,**k:calls.append(a))
    with pytest.raises(PermissionError):adapter.create('foreign',2)
    assert calls==[]


def test_deleted_instance_cannot_reuse_token_from_stale_in_memory_handle(tmp_path,monkeypatch):
    adapter=Adapter(config(tmp_path))
    monkeypatch.setattr(adapter,'_control',lambda *a,**k:response({'sandboxID':'s','envdAccessToken':'stale-secret'}))
    handle=adapter.create('run',2);adapter.delete(handle,2)
    calls=[]
    monkeypatch.setattr(adapter,'_http',lambda *a,**k:calls.append(a))
    with pytest.raises(ValueError):adapter.execute(handle,'true',2)
    assert calls==[]


def test_async_delete_remains_observable_but_cannot_restore_execution_credentials(tmp_path,monkeypatch):
    adapter=Adapter(config(tmp_path));adapter.cluster={'group':'g'}
    adapter._instance_vault.save('s',{'routeKey':'route-secret','envdAccessToken':'data-secret'})
    calls=[]
    def http(method,url,**kwargs):
        calls.append((method,kwargs))
        if method=='GET':return response({'sandboxID':'s','state':'deleting','routeKey':'route-secret','envdAccessToken':'data-secret'})
        return response({})
    monkeypatch.setattr(adapter,'_http',http)
    adapter.delete({'sandbox_id':'s'},2)
    result=adapter.inspect({'sandbox_id':'s'},2)
    assert result['state']=='deleting' and calls[-1][1]['headers']['X-Kuasar-Route-Key']=='route-secret'
    assert 'route-secret' not in str(result) and 'data-secret' not in str(result)
    with pytest.raises(ValueError):adapter.execute({'sandbox_id':'s'},'true',2)
