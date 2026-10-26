import pytest
from sandbox_graph.environment.secrets import SecretStore
from sandbox_bench.adapters import Adapter


def config(tmp_path):
    store=SecretStore(tmp_path/'private')
    item=store.create_if_absent('env1','control',lambda:'a'*40,version='v1')
    return store,{'product':'agentenv','api_url':'http://localhost:1','data_url':'http://localhost:2',
        'auth_ref':item.ref,'auth_scope':'env1','auth_version':'v1','server_credential_version':'v1',
        'secret_store_path':str(store.root)}


def test_benchmark_resolves_private_ref_only_at_executor_boundary(tmp_path):
    store,cfg=config(tmp_path)
    adapter=Adapter(cfg)
    assert adapter.headers['X-API-Key']=='a'*40
    assert 'a'*40 not in str(adapter.config)
    assert adapter.config['auth_ref'].startswith('secret://')


@pytest.mark.parametrize('change',[{'auth_scope':'wrong'},{'auth_version':'old'},
    {'server_credential_version':'v2'},{'secret_store_path':None},{'api_key_env':'OLD_KEY'}])
def test_stale_ambiguous_or_wrong_scope_credential_does_not_fall_back(tmp_path,change,monkeypatch):
    store,cfg=config(tmp_path);monkeypatch.setenv('OLD_KEY','a'*40)
    with pytest.raises((ValueError,PermissionError)):Adapter(cfg|change)
