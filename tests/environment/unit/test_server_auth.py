import hashlib
import json
import pytest

from sandbox_graph.environment.secrets import SecretStore


def test_shared_agentenv_key_reuses_version_and_preserves_source(tmp_path):
    from sandbox_graph.environment.products.server_auth import prepare_auth
    store=SecretStore(tmp_path/'secrets');ref=store.put('group','api','A'*48).ref
    a=prepare_auth('agentenv',store=store,scope='group',credential_ref=ref,
        observed={'environment':{'AENV_API_KEY':'old'*16},'external':None},output=tmp_path/'a')
    b=prepare_auth('agentenv',store=store,scope='group',credential_ref=ref,
        observed={'environment':{},'external':None},output=tmp_path/'b')
    assert a['credential_ref']==b['credential_ref']==ref
    assert a['carrier']=='environment' and b['carrier']=='managed_file'
    assert (tmp_path/'a').read_bytes()==b"AENV_API_KEY='"+b'A'*48+b"'\n"
    assert (tmp_path/'b').read_bytes()==b'A'*48
    assert (tmp_path/'a').stat().st_mode&0o777==0o600
    assert 'A'*48 not in json.dumps(a)
    assert prepare_auth('agentenv',store=store,scope='group',credential_ref=ref,
        observed={'environment':{},'external':None},output=tmp_path/'b')==b


def test_cube_preserves_callback_and_rejects_conflicting_start_override(tmp_path):
    from sandbox_graph.environment.products.server_auth import prepare_auth
    store=SecretStore(tmp_path/'secrets');ref=store.put('group','api','A'*48).ref
    result=prepare_auth('cubesandbox',store=store,scope='group',credential_ref=ref,
        observed={'environment':{'AUTH_CALLBACK_URL':'https://auth.internal/verify'},'argv':[]},output=tmp_path/'callback')
    assert result['carrier']=='callback' and result['asset'] is None
    assert not (tmp_path/'callback').exists()
    with pytest.raises(ValueError,match='override'):
        prepare_auth('cubesandbox',store=store,scope='group',credential_ref=ref,
            observed={'environment':{},'argv':['--cube-api-key','different']},output=tmp_path/'key')


def test_kuasar_pair_check_before_add_never_refreshes_existing_ttl():
    from sandbox_graph.environment.products.server_auth import ensure_kuasar_tenant,kuasar_pair
    pair=kuasar_pair('01'*32)
    evidence={'api_secret_fingerprint':hashlib.sha256(bytes.fromhex(pair['api_secret'])).hexdigest(),
        'manifest_key_fingerprint':hashlib.sha256(bytes.fromhex(pair['manifest_key'])).hexdigest()}
    calls=[]
    def present(body):
        calls.append(body)
        return {'op':body['op'],'status':'present',**evidence}
    assert ensure_kuasar_tenant(pair,call=present,authorize=lambda:True)['created'] is False
    assert [v['op'] for v in calls]==['check']
    calls.clear()
    def absent(body):
        calls.append(body)
        return {'op':body['op'],'status':('absent' if len(calls)==1 else 'added' if body['op']=='add' else 'present'),**evidence}
    assert ensure_kuasar_tenant(pair,call=absent,authorize=lambda:True)['created'] is True
    assert [v['op'] for v in calls]==['check','add','check']
    assert calls[1]['ttl_seconds']==0
    assert pair['manifest_key'] not in json.dumps(ensure_kuasar_tenant(pair,call=present,authorize=lambda:True))


def test_unknown_check_and_revocation_never_add():
    from sandbox_graph.environment.products.server_auth import ensure_kuasar_tenant,kuasar_pair
    pair=kuasar_pair('01'*32);calls=[]
    def unknown(body):calls.append(body);return {'status':'absent','error':'backend failed'}
    with pytest.raises(ValueError):ensure_kuasar_tenant(pair,call=unknown,authorize=lambda:True)
    assert len(calls)==1
    with pytest.raises(PermissionError):ensure_kuasar_tenant(pair,call=unknown,authorize=lambda:False)
    assert len(calls)==1


def test_cluster_group_preserves_spec_and_rejects_duplicate_identity():
    from sandbox_graph.environment.products.server_auth import kuasar_group,kuasar_pair
    pair=kuasar_pair('01'*32)
    value=kuasar_group({'group':'g','sandbox_config':{'cpu':'2','memory':'4GiB'}},pair=pair,existing_groups=['other'])
    assert value['sandbox_config']=={'cpu':'2','memory':'4GiB'}
    assert value['manifest_key']=={'type':'inline','value':'01'*32}
    with pytest.raises(ValueError):kuasar_group(value,pair=pair,existing_groups=['g'])


def test_server_auth_recipe_binds_exact_version_and_restarts_only_declared_service(tmp_path):
    from sandbox_graph.environment.products.server_auth import prepare_auth,deployment_recipe
    store=SecretStore(tmp_path/'secrets');ref=store.put('group','api','A'*48).ref
    manifest=prepare_auth('agentenv',store=store,scope='group',credential_ref=ref,
        observed={'environment':{'AENV_API_KEY':'B'*48}},output=tmp_path/'private.env')
    config=deployment_recipe(manifest,scope='group',secret_root=str(store.root),
        target='/etc/agentenv/auth.env',unit='agentenv.service',expected_before=None,
        dropin_before=None,output=tmp_path/'dropin')
    assert config['client']['auth_ref']==ref and config['client']['server_credential_version']==ref.rsplit('/',1)[1]
    assert config['expected_before']['/etc/agentenv/auth.env'] is None
    assert [s['argv'] for s in config['steps']][-2:]==[
        ('systemctl','daemon-reload'),('systemctl','restart','agentenv.service')]
    assert (tmp_path/'dropin').read_text()=='[Service]\nEnvironmentFile=/etc/agentenv/auth.env\n'
    assert 'A'*48 not in json.dumps(config)
    with pytest.raises(PermissionError):
        deployment_recipe(manifest,scope='another',secret_root=str(store.root),target='/etc/agentenv/auth.env',
            unit='agentenv.service',expected_before=None,dropin_before=None,output=tmp_path/'other')


def test_kuasar_cluster_client_resolves_scoped_private_api_secret_and_mints_mac(tmp_path):
    from sandbox_bench.adapters import Adapter
    import hmac
    store=SecretStore(tmp_path/'secrets');secret='02'*32
    ref=store.put('group','api-secret',secret).ref
    binding={'auth_ref':ref,'auth_scope':'group','auth_version':'v1','server_credential_version':'v1',
        'secret_store_path':str(store.root)}
    config={'product':'kuasar','api_url':'http://localhost:8080','data_url':'http://localhost:8081',
        'kuasar_cluster':{'api_host':'api.example','group':'bench','domain':'example','credential':binding}}
    adapter=Adapter(config)
    raw=bytes.fromhex(adapter.headers['X-API-Key'].removeprefix('e2b_'))
    assert raw[:12]==hashlib.sha256(bytes.fromhex(secret)).digest()[:12]
    assert raw[-16:]==hmac.new(bytes.fromhex(secret),raw[:-16],hashlib.sha256).digest()[:16]
    assert secret not in json.dumps(adapter.config)
    config['kuasar_cluster']['api_secret_env']='KEY'
    with pytest.raises(ValueError,match='exclusive'):Adapter(config)
    del config['kuasar_cluster']['api_secret_env']
    binding['server_credential_version']='v2'
    with pytest.raises(ValueError,match='versions'):Adapter(config)
