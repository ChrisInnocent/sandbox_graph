import json
import pytest
from sandbox_graph.environment.instance_tokens import InstanceTokenVault


def test_private_instance_tokens_survive_restart_and_never_cross_instance_or_scope(tmp_path):
    first=InstanceTokenVault(tmp_path,scope='work:a',generation=1)
    ref=first.save('sandbox/1',{'envdAccessToken':'secret-one','routeKey':'route-one'})
    first.save('sandbox/2',{'envdAccessToken':'secret-two'})
    restarted=InstanceTokenVault(tmp_path,scope='work:a',generation=1)
    assert restarted.load('sandbox/1')['envdAccessToken']=='secret-one'
    assert restarted.load('sandbox/2')['envdAccessToken']=='secret-two'
    assert 'secret-one' not in ref and ref.startswith('secret://')
    with pytest.raises(KeyError):InstanceTokenVault(tmp_path,scope='work:b',generation=1).load('sandbox/1')
    assert restarted.owned()==['sandbox/1','sandbox/2']


def test_new_generation_invalidates_old_adapter_and_closed_instances(tmp_path):
    old=InstanceTokenVault(tmp_path,scope='work:a',generation=1)
    old.save('sandbox',{'envdAccessToken':'old'})
    new=InstanceTokenVault(tmp_path,scope='work:a',generation=2)
    with pytest.raises(PermissionError):old.load('sandbox')
    with pytest.raises(KeyError):new.load('sandbox')
    new.save('sandbox',{'envdAccessToken':'new'});new.close('sandbox')
    with pytest.raises(KeyError):new.load('sandbox')
    assert new.owned()==[]
    with pytest.raises(PermissionError):InstanceTokenVault(tmp_path,scope='work:a',generation=1)


def test_token_refresh_preserves_versions_and_existing_generation_cannot_resurrect_deleted_id(tmp_path):
    vault=InstanceTokenVault(tmp_path,scope='work',generation=1)
    before=vault.save('s',{'trafficAccessToken':'before'})
    after=vault.save('s',{'trafficAccessToken':'after'})
    assert before!=after and vault.load('s')['trafficAccessToken']=='after'
    vault.close('s')
    with pytest.raises(PermissionError):vault.save('s',{'trafficAccessToken':'another-instance'})
