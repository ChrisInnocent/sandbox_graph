import json
from types import SimpleNamespace
import pytest

SPEC={'schema_version':1,'product':'agentenv','role':'gateway','name':'aenv-gateway',
      'image':'registry/gateway@sha256:'+'a'*64,
      'mounts':[{'source':'/etc/aenv/gateway.json','target':'/config/default.json'},
                {'source':'/etc/aenv/api-key','target':'/run/secrets/api-key'}],
      'args':['-config','/config/default.json']}


class Docker:
    def __init__(self,container=None):self.calls=[];self.container=container
    def __call__(self,argv,**kwargs):
        self.calls.append(argv)
        if argv[:3]==['docker','image','inspect']:
            return SimpleNamespace(returncode=0,stdout=json.dumps([{'Id':'sha256:image','RepoDigests':[SPEC['image']]}]))
        if argv[:2]==['docker','inspect']:
            return SimpleNamespace(returncode=0 if self.container else 1,stdout=json.dumps([self.container]) if self.container else '')
        if argv[:2]==['docker','rm']:self.container=None
        if argv[:2]==['docker','run']:
            self.container={'Id':'new','Name':'/aenv-gateway','Image':'sha256:image','State':{'Running':True},
                'Config':{'Labels':{'sandbox-graph.product':'agentenv','sandbox-graph.role':'gateway'}}}
            return SimpleNamespace(returncode=0,stdout='new\n')
        return SimpleNamespace(returncode=0,stdout='')


def test_owned_container_replacement_uses_frozen_id_and_never_pulls():
    from sandbox_graph.environment.products.container_action import activate
    docker=Docker({'Id':'old','Name':'/aenv-gateway','Config':{'Labels':{
        'sandbox-graph.product':'agentenv','sandbox-graph.role':'gateway'}}})
    result=activate(SPEC,expected_id='old',runner=docker)
    assert result['container_id']=='new'
    assert ['docker','rm','--force','old'] in docker.calls
    launch=next(call for call in docker.calls if call[:2]==['docker','run'])
    assert '--pull=never' in launch and '--label' in launch and 'aenv-gateway' in launch


@pytest.mark.parametrize('fault',['id','owner','image'])
def test_container_drift_is_rejected_before_mutation(fault):
    from sandbox_graph.environment.products.container_action import activate
    docker=Docker({'Id':'old','Name':'/aenv-gateway','Config':{'Labels':{
        'sandbox-graph.product':'agentenv','sandbox-graph.role':'gateway'}}})
    expected='other' if fault=='id' else 'old'
    if fault=='owner':docker.container['Config']['Labels']={}
    def runner(argv,**kwargs):
        if fault=='image' and argv[:3]==['docker','image','inspect']:return SimpleNamespace(returncode=1,stdout='')
        return docker(argv,**kwargs)
    with pytest.raises((ValueError,PermissionError)):activate(SPEC,expected_id=expected,runner=runner)
    assert not any(call[1] in {'run','rm'} for call in docker.calls)


def test_offline_image_archive_is_hashed_and_local_content_id_verified(tmp_path):
    import hashlib
    from sandbox_graph.environment.products.container_action import load_image
    path=tmp_path/'image.tar';path.write_bytes(b'synthetic archive; Docker boundary is controlled')
    spec={'archive':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'image':'sha256:'+'b'*64}
    calls=[];loaded=False
    def docker(argv,**kwargs):
        nonlocal loaded
        calls.append(argv)
        if argv[:2]==['docker','load']:loaded=True;return SimpleNamespace(returncode=0,stdout='loaded')
        return SimpleNamespace(returncode=0 if loaded else 1,stdout=json.dumps([{'Id':spec['image'],'RepoDigests':[]}]))
    assert load_image(spec,docker)['reused'] is False
    assert load_image(spec,docker)['reused'] is True
    assert len([c for c in calls if c[:2]==['docker','load']])==1
    with pytest.raises(ValueError):load_image(spec|{'sha256':'c'*64},docker)


def test_post_mutation_malformed_observation_remains_unknown():
    from sandbox_graph.environment.products.container_action import activate
    docker=Docker()
    def runner(argv,**kwargs):
        if argv[:2]==['docker','inspect'] and docker.container:
            return SimpleNamespace(returncode=0,stdout='not JSON')
        return docker(argv,**kwargs)
    with pytest.raises(RuntimeError,match='unresolved'):
        activate(SPEC,expected_id=None,runner=runner)
