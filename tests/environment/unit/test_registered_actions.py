import hashlib
from pathlib import Path
from sandbox_graph.environment.scripts.service_action import ServiceAction, ServiceActionRegistry
from sandbox_graph.environment.scripts.product_install import InstallAction, ProductInstallRegistry


def test_service_action_uses_fixed_argv_and_requires_postcondition(monkeypatch):
    calls=[]
    def run(argv,**kwargs):
        calls.append(argv)
        class Result: returncode=0; stdout=''; stderr=''
        result=Result()
        if argv[1]=='is-active': result.returncode=3
        return result
    registry=ServiceActionRegistry((ServiceAction(action_ref='cube.service.start.v1',unit='cubelet.service',verb='start'),),runner=run)
    result=registry.execute('cube.service.start.v1')
    assert result['status']=='postcondition_failed'
    assert calls==[['systemctl','start','cubelet.service'],['systemctl','is-active','--quiet','cubelet.service']]
    assert registry.execute('not.registered')=={'status':'blocked','reason':'action_not_registered'}


def test_product_install_digest_lock_and_exit_zero_not_acceptance(tmp_path):
    artifact=tmp_path/'install.bin'; artifact.write_bytes(b'locked package')
    digest=hashlib.sha256(artifact.read_bytes()).hexdigest()
    def run(argv,**kwargs):
        class Result: returncode=0; stdout=''; stderr=''
        return Result()
    registry=ProductInstallRegistry((InstallAction(action_ref='kuasar.install.v1',artifact_path=str(artifact),
        artifact_sha256=digest,argv=('/bin/true',),timeout_seconds=10,verification_rule='probe-unit'),),
        runner=run,verify=lambda rule:False)
    assert registry.execute('kuasar.install.v1')['status']=='postcondition_failed'
    artifact.write_bytes(b'changed')
    assert registry.execute('kuasar.install.v1')['status']=='artifact_digest_mismatch'
