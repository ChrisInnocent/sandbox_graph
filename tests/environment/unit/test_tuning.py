import pytest
from sandbox_graph.environment.contracts import TargetValue


def test_kuasar_tuning_preserves_unrelated_fields_and_never_changes_guest_spec():
    from sandbox_graph.environment.tuning import tune
    base={'sandbox':{'capacity':20,'vcpu':2,'memory':'4GiB'},'custom':{'operator':'keep'}}
    changed=tune('kuasar','conductor',base,{'sandbox_capacity':TargetValue(mode='exact',value=50,unit='count')})
    assert changed['values']=={'sandbox':{'capacity':50,'vcpu':2,'memory':'4GiB'},'custom':{'operator':'keep'}}
    assert base['sandbox']['capacity']==20
    with pytest.raises(ValueError):tune('kuasar','conductor',base,{'guest_memory':TargetValue(mode='exact',value=8,unit='GiB')})


def test_relaxation_uses_named_budget_and_rejects_missing_or_wrong_binding():
    from sandbox_graph.environment.tuning import tune
    target=TargetValue(mode='relax_for_plan',budget_ref='artifact://budget/experiment')
    budget={'minimum':10,'maximum':100,'step':20,'unit':'count','budget_ref':target.budget_ref}
    result=tune('kuasar','conductor',{}, {'sandbox_capacity':target},budgets={target.budget_ref:budget})
    assert result['values']['sandbox']['capacity']==90
    with pytest.raises(ValueError):tune('kuasar','conductor',{}, {'sandbox_capacity':target})
    with pytest.raises(ValueError):tune('kuasar','conductor',{}, {'sandbox_capacity':target},budgets={target.budget_ref:budget|{'budget_ref':'artifact://other'}})


def test_cube_and_agentenv_apply_audited_paths_and_units():
    from sandbox_graph.environment.tuning import tune
    assert tune('cubesandbox','cubelet',{}, {'quota_cpu':TargetValue(mode='exact',value=4,unit='CPU')})['values']['host']['quota']['mcpu_limit']==4000
    assert tune('agentenv','scheduler',{}, {'max_sandbox_count':TargetValue(mode='exact',value=0,unit='count')})['values']['scheduler']['node_resource_limit']['max_sandbox_count']==0
    with pytest.raises(ValueError):tune('kuasar','conductor',{}, {'admission_rate':TargetValue(mode='exact',value=1.5,unit='requests/s')})


def test_tuning_asset_is_private_digest_locked_and_reusable_by_cas_recipe(tmp_path):
    from sandbox_graph.environment.tuning import prepare_asset
    from sandbox_graph.environment.config import load_config
    import hashlib,stat
    source=tmp_path/'conductor.yaml';source.write_text('sandbox:\n  capacity: 20\n  memory: 4GiB\n')
    before=source.read_bytes();output=tmp_path/'asset.yaml'
    result=prepare_asset('kuasar','conductor',source,output,remote_path='/etc/node-ctl/conductor.yaml',
        targets={'sandbox_capacity':{'mode':'exact','value':100,'unit':'count'}})
    assert source.read_bytes()==before and load_config(output)['sandbox']=={'capacity':100,'memory':'4GiB'}
    assert stat.S_IMODE(output.stat().st_mode)==0o600
    assert result['asset']['sha256']==hashlib.sha256(output.read_bytes()).hexdigest()
    assert result['expected_before']['/etc/node-ctl/conductor.yaml']==hashlib.sha256(before).hexdigest()
    assert result['step']['kind']=='file' and result['evidence_level']=='rendered_only'


def test_uint64_limit_is_not_rounded_through_float():
    from sandbox_graph.environment.tuning import tune
    maximum=2**64-1
    actual=tune('agentenv','scheduler',{}, {'max_allocated_memory_bytes_including_paused':
        TargetValue(mode='exact',value=maximum,unit='B')})['values']
    assert actual['scheduler']['node_resource_limit']['max_allocated_memory_bytes_including_paused']==maximum


def test_master_fraction_and_agent_native_toml_preserve_unrelated_configuration(tmp_path):
    from sandbox_graph.environment.tuning import tune,prepare_asset
    from sandbox_graph.environment.config import load_config
    actual=tune('cubesandbox','CubeMaster',{'scheduler':{'max_mvm_memory':'4Gi'}},
        {'reserve_ratio':TargetValue(mode='exact',value=.75,unit='ratio')})['values']
    assert actual=={'scheduler':{'max_mvm_memory':'4Gi','node_max_mvm_num_reserve_num_percent':.75}}
    source=tmp_path/'config.toml';source.write_text('# operator comment\n[template_build]\nmax_concurrent_builds = 4 # keep\nbuilder_cpu_count = 16\n')
    output=tmp_path/'config.next.toml'
    kwargs={'remote_path':'/etc/aenv/config.toml','targets':{'max_builds':TargetValue(mode='exact',value=8,unit='count')}}
    one=prepare_asset('agentenv','server',source,output,**kwargs)
    assert load_config(output)['template_build']=={'max_concurrent_builds':8,'builder_cpu_count':16}
    assert '# operator comment' in output.read_text() and '# keep' in output.read_text()
    assert prepare_asset('agentenv','server',source,output,**kwargs)==one
