import hashlib
from types import SimpleNamespace
from sandbox_graph.environment.products.probe import probe_product


def runner(argv,**kwargs):
    assert argv[:2]==['systemctl','show']
    return SimpleNamespace(returncode=0,stdout='LoadState=loaded\nActiveState=active\nSubState=running\nMainPID=42\n',stderr='')


def test_real_files_and_role_units_are_checked_without_mutation(tmp_path):
    binary=tmp_path/'server';binary.write_bytes(b'locked executable')
    daemon=tmp_path/'daemon';daemon.write_bytes(b'daemon')
    result=probe_product('agentenv',roles=('server',),paths={'server':str(binary),'uvm-ublk-daemon':str(daemon)},
        expected_hashes={'server':hashlib.sha256(binary.read_bytes()).hexdigest(),
            'uvm-ublk-daemon':hashlib.sha256(daemon.read_bytes()).hexdigest()},runner=runner)
    assert result['installation']=='installed'
    assert result['services'][0]['unit']=='aenv.service'
    assert result['files']['server']['status']=='matched'
    assert result['functional_ready'] is False  # registration/auth/canary are separate evidence


def test_missing_and_partial_deployments_are_distinguished(tmp_path):
    paths={'server':str(tmp_path/'server'),'uvm-ublk-daemon':str(tmp_path/'daemon')}
    inactive=lambda *a,**k:SimpleNamespace(returncode=0,stdout='LoadState=not-found\nActiveState=inactive\nMainPID=0\n',stderr='')
    assert probe_product('agentenv',roles=('server',),paths=paths,expected_hashes={},runner=inactive)['installation']=='absent'
    (tmp_path/'server').write_bytes(b'x')
    result=probe_product('agentenv',roles=('server',),paths=paths,expected_hashes={},runner=inactive)
    assert result['installation']=='partial'
    assert result['files']['server']['status']=='unverified_version'


def test_reused_symlink_or_failed_service_probe_is_unknown(tmp_path):
    (tmp_path/'server').symlink_to('/bin/sh')
    result=probe_product('agentenv',roles=('server',),paths={'server':str(tmp_path/'server'),'uvm-ublk-daemon':str(tmp_path/'absent')},
        expected_hashes={},runner=lambda *a,**k:SimpleNamespace(returncode=1,stdout='',stderr='private remote text'))
    assert result['installation']=='unknown'
    assert 'private remote text' not in str(result)


def test_kuasar_conductor_and_proxy_remain_separate_services(tmp_path):
    result=probe_product('kuasar',roles=('conductor','proxy'),paths={'node-ctl':str(tmp_path/'absent')},expected_hashes={},runner=runner)
    assert {row['unit'] for row in result['services']}=={'node-ctl.service','node-proxy.service'}
    assert result['installation']=='partial'


def test_agentenv_control_roles_verify_container_digest_without_host_binary_requirement():
    import json
    digest='sha256:'+'a'*64
    def inspect(argv,**kwargs):
        assert argv[:2]==['docker','inspect']
        return SimpleNamespace(returncode=0,stdout=json.dumps([{'State':{'Status':'running'},'Image':digest}]),stderr='')
    result=probe_product('agentenv',roles=('scheduler','gateway'),paths={},
        expected_hashes={'scheduler':digest,'gateway':digest},runner=inspect)
    assert result['installation']=='installed' and result['files']=={}
    result=probe_product('agentenv',roles=('gateway',),paths={},expected_hashes={'gateway':'sha256:'+'b'*64},runner=inspect)
    assert result['installation']=='partial' and result['containers'][0]['status']=='mismatch'


def test_cube_control_proxy_is_a_digest_pinned_container_not_wrapper_mainpid(tmp_path):
    import json
    names=('CubeOps','CubeMaster','CubeAPI','CubeTemplateCenter');paths={};hashes={}
    for name in names:
        path=tmp_path/name;path.write_bytes(name.encode());paths[name]=str(path)
        hashes[name]=hashlib.sha256(name.encode()).hexdigest()
    hashes['cubeproxy']='a'*64
    def inspect(argv,**kwargs):
        if argv[:2]==['systemctl','show']:return runner(argv,**kwargs)
        assert argv==['docker','inspect','cube-proxy']
        return SimpleNamespace(returncode=0,stdout=json.dumps([{'State':{'Status':'running','Pid':77},'Image':'sha256:'+'a'*64,'Id':'container'}]))
    result=probe_product('cubesandbox',roles=('control',),paths=paths,expected_hashes=hashes,runner=inspect)
    assert result['installation']=='installed' and 'cubeproxy' not in result['files']
    assert result['containers'][0]['pid']==77


def test_cube_compute_uses_embedded_vmm_without_separate_ch_binary(tmp_path):
    paths={};hashes={}
    for name in ('cubelet','cube-shim'):
        binary=tmp_path/name;binary.write_bytes(name.encode());paths[name]=str(binary)
        hashes[name]=hashlib.sha256(binary.read_bytes()).hexdigest()
    result=probe_product('cubesandbox',roles=('compute',),paths=paths,expected_hashes=hashes,runner=runner)
    assert result['installation']=='installed'
    assert set(result['files'])=={'cubelet','cube-shim'}
