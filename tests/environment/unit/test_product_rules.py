"""Source-derived numeric and role vectors; no product installation is simulated."""
import pytest
from sandbox_graph.environment.products import cubesandbox as cube, kuasar, agentenv


def test_cube_uses_reported_capacity_and_reserve_multiplier_not_minimum():
    assert cube.effective_limits(reported_max=200,configured_max=100,reserve_percent=.9,
        reported_concurrency=20,configured_concurrency=100,quota_mem_mib=9450,
        reserved_mem_mib=10240,api_rate=0)=={
            'max_sandboxes':200,'admission_sandboxes':180,'create_concurrency':100,
            'reserved_mem_mib':945,'api_rate':1}
    assert cube.effective_limits(reported_max=0,configured_max=0,reserve_percent=.5,
        reported_concurrency=0,configured_concurrency=0,quota_mem_mib=0,
        reserved_mem_mib=0,api_rate=100)['max_sandboxes']==3000


def test_kuasar_zero_capacity_differs_from_admission_defaults():
    value=kuasar.effective_limits({'sandbox':{'capacity':0},'resource_listen':{'admission':{
        'rate':0,'burst':0,'queue_max_depth':0}}})
    assert value=={'sandbox_capacity':None,'admission_rate':4,'admission_burst':16,
        'queue_max_depth':256,'max_builds':2}
    with pytest.raises(ValueError):kuasar.effective_limits({'builder':{'admission':{'execution':{'max_builds':0}}}})
    # orchestrator/config/config.go ResourceAdmissionConfig.Rate is int.
    with pytest.raises(ValueError):kuasar.effective_limits({'resource_listen':{'admission':{'rate':0.5}}})


def test_agentenv_nil_zero_equality_and_paused_are_distinct():
    snapshot={'sandbox_count':3,'sandbox_starting_count':1,'paused_sandbox_count':2}
    assert agentenv.scheduler_eligibility(snapshot,{'max_sandbox_count':3})['eligible']
    assert not agentenv.scheduler_eligibility(snapshot,{'max_sandbox_count':0})['eligible']
    assert not agentenv.scheduler_eligibility(snapshot,{'max_sandbox_count_including_paused':4})['eligible']
    assert agentenv.scheduler_eligibility(snapshot,None)['eligible']
    assert agentenv.scheduler_eligibility(None,None)=={'eligible':None,'ready':False,'reason':'node_report_missing'}


def test_agentenv_api_key_empty_environment_must_not_fall_back():
    key='a'*32
    assert agentenv.credential_source(env={},external=key,managed=None)['source']=='external'
    assert agentenv.credential_source(env={'AENV_API_KEY':key},external=None,managed=None)['source']=='environment'
    for value in ('','short','bad value'*8):
        with pytest.raises(ValueError):agentenv.credential_source(env={'AENV_API_KEY':value},external=key,managed=key)


def test_cube_callback_precedence_and_auth_carrier_are_explicit():
    assert cube.auth_mode(callback_url='https://auth',static_key_present=True)=='callback'
    assert cube.auth_mode(callback_url=None,static_key_present=True)=='static'
    assert cube.auth_mode(callback_url=None,static_key_present=False)=='unprotected'
    assert cube.AUTH_HEADER==kuasar.AUTH_HEADER==agentenv.AUTH_HEADER=='X-API-Key'


def assets(*names):
    return {name:{'path':'/staged/'+name,'sha256':'a'*64} for name in names}


def test_cube_install_requires_exact_role_and_reuse_never_resets_configuration():
    a=assets('oneclick','package','dependency-lock','rendered-env')
    with pytest.raises(ValueError,match='control includes compute'):
        cube.install_recipe(roles=('control',),assets=a,state='absent',root='/opt/cube',config_path='/etc/cube.env')
    assert cube.install_recipe(roles=('control','compute'),assets=a,state='installed',root='/opt/cube',config_path='/etc/cube.env')==()
    recipe=cube.install_recipe(roles=('compute',),assets=a,state='absent',root='/usr/local/services/cubetoolbox',config_path='/etc/cube.env')
    assert recipe[-1].argv[-2:]==('--mode=install','--yes')
    assert recipe[-1].environment['ONE_CLICK_DEPLOY_ROLE']=='compute'
    assert recipe[-1].network=='none'
    assert any(step.asset==a['package'] for step in recipe)
    assert any(step.asset==a['dependency-lock'] for step in recipe)
    assert any(step.target=='/etc/cube.env' and step.mode==0o600 for step in recipe)
    with pytest.raises(ValueError,match='fixed'):
        cube.install_recipe(roles=('compute',),assets=a,state='absent',root='/opt/cube',config_path='/etc/cube.env')
    with pytest.raises(ValueError):cube.install_recipe(roles=('compute',),assets=a,state='unknown',root='/opt/cube',config_path='/etc/cube.env')


def test_kuasar_native_services_and_config_parser_are_source_commands():
    a=assets('node-ctl','sandbox-ctl','connector','guest-runtime','runtime-assets','node-ctl.service','node-proxy.service','conductor.yaml','proxy.yaml')
    recipe=kuasar.install_recipe(roles=('conductor','proxy','runtime'),assets=a,state='absent')
    argv=[step.argv for step in recipe]
    assert ('/usr/bin/node-ctl','config','conductor','--config','/etc/node-ctl/conductor.yaml','--resolve') in argv
    assert ('systemctl','start','node-ctl.service') in argv
    assert argv.index(('systemctl','start','node-ctl.service'))<argv.index(('systemctl','start','node-proxy.service'))
    with pytest.raises(ValueError):kuasar.install_recipe(roles=('conductor','proxy'),assets=assets('node-ctl'),state='absent')


def test_agentenv_install_is_pinned_and_uses_dedicated_account():
    a=assets('server','aenv','aenv-buildctl','uvm-ublk-daemon','runtime-assets','aenv.service','config.toml','aenv.env','api-key')
    recipe=agentenv.install_recipe(roles=('server',),assets=a,state='absent')
    argv=[step.argv for step in recipe]
    assert ('/usr/local/bin/server','--setup-host','--runtime-user','aenv','--runtime-group','aenv') in argv
    assert all('latest' not in ' '.join(row) and 'curl' not in row for row in argv)
    assert 'KillMode=process' in agentenv.server_unit()
    assert 'User=aenv' in agentenv.server_unit()
    # The account command creates its home; extraction requires an absent target.
    archive=next(i for i,step in enumerate(recipe) if step.kind=='archive')
    account=next(i for i,step in enumerate(recipe) if step.step_id=='account:aenv')
    assert archive<account
    with pytest.raises(ValueError):agentenv.install_recipe(roles=('gateway',),assets={},state='absent',images={'gateway':'x:latest'})


def test_cluster_endpoints_require_real_cross_host_identity_and_digest_images():
    with pytest.raises(ValueError):agentenv.control_config([{'id':'n','endpoint':'http://server:8000'}],multi_host=True)
    assert agentenv.control_config([{'id':'n','endpoint':'http://10.0.0.2:8000'}],multi_host=True)['scheduler']['nodes'][0]['id']=='n'


def test_kuasar_group_import_preserves_foreign_rows_and_rejects_duplicate_source():
    existing={'import_groups':[{'source_type':'file','source_id':'foreign','path':'/etc/foreign'}]}
    configured=kuasar.group_import(existing,source_id='managed',directory='/etc/kuasar/managed')
    assert configured['import_groups'][0]==existing['import_groups'][0]
    assert configured['import_groups'][1]=={'source_type':'file','source_id':'managed','path':'/etc/kuasar/managed'}
    assert kuasar.group_import(configured,source_id='managed',directory='/etc/kuasar/managed')==configured
    with pytest.raises(ValueError):kuasar.group_import(configured,source_id='managed',directory='/elsewhere')


def test_native_install_copies_configuration_before_parser_or_setup():
    a=assets('node-ctl','node-ctl.service','conductor.yaml')
    recipe=kuasar.install_recipe(roles=('conductor',),assets=a,state='absent')
    target=next(i for i,s in enumerate(recipe) if s.target=='/etc/node-ctl/conductor.yaml')
    check=next(i for i,s in enumerate(recipe) if s.step_id=='validate:conductor')
    assert target<check
    a=assets('server','aenv','aenv-buildctl','uvm-ublk-daemon','runtime-assets','aenv.service','config.toml','aenv.env','api-key')
    recipe=agentenv.install_recipe(roles=('server',),assets=a,state='absent')
    for target in ('/etc/aenv/config.toml','/etc/aenv/aenv.env','/var/lib/aenv/secrets/api-key'):
        assert next(i for i,s in enumerate(recipe) if s.target==target)<next(i for i,s in enumerate(recipe) if s.step_id=='setup:assets')


def test_explicit_cube_control_only_recipe_does_not_install_compute_or_run_oneclick():
    from sandbox_graph.environment.products.probe import UNITS
    names=('CubeOps','CubeMaster','CubeAPI','CubeTemplateCenter')
    a=assets(*names,*('config:'+name for name in names),'cubeproxy.compose','dependency-lock',*UNITS['cubesandbox']['control'])
    recipe=cube.install_recipe(roles=('control',),assets=a,state='absent',root='/opt/cube',config_path='/unused',
        control_profile={'config_paths':{name:'/etc/cube/'+name+'.yaml' for name in names},'proxy_compose_path':'/etc/cube/proxy.yaml'})
    assert all('cubelet' not in str(step).lower() and 'install.sh' not in str(step) for step in recipe)
    assert any(step.kind=='cube-compose' for step in recipe)


def test_partial_native_install_repairs_binary_and_activates_affected_service():
    a=assets('node-ctl','node-ctl.service','conductor.yaml')
    full=kuasar.install_recipe(roles=('conductor',),assets=a,state='absent')
    observations={step.step_id:'completed' for step in full};observations['file:node-ctl']='missing'
    repair=kuasar.install_recipe(roles=('conductor',),assets=a,state='partial',observed_steps=observations)
    assert [step.step_id for step in repair]==['file:node-ctl','validate:conductor','start:conductor']
    assert repair[-1].argv==('systemctl','restart','node-ctl.service')
    observations['file:conductor.yaml']='unknown'
    with pytest.raises(ValueError,match='unresolved'):
        kuasar.install_recipe(roles=('conductor',),assets=a,state='partial',observed_steps=observations)


@pytest.mark.parametrize('missing,affected',[
    ('node-ctl',('conductor','proxy')),('conductor.yaml',('conductor',)),
    ('node-ctl.service',('conductor',)),
])
def test_kuasar_partial_repair_follows_role_dependencies(missing,affected):
    a=assets('node-ctl','node-ctl.service','node-proxy.service','conductor.yaml','proxy.yaml')
    full=kuasar.install_recipe(roles=('conductor','proxy'),assets=a,state='absent')
    observations={step.step_id:'completed' for step in full}
    observations['file:'+missing]='missing'
    repair=kuasar.install_recipe(roles=('conductor','proxy'),assets=a,state='partial',observed_steps=observations)
    starts=[s for s in repair if s.step_id.startswith('start:')]
    assert [s.step_id for s in starts]==['start:'+role for role in affected]
    assert all(s.argv[1]=='restart' for s in starts)
    assert all(s.step_id!='file:proxy.yaml' for s in repair)
    ids=[s.step_id for s in repair]
    assert ids[0]=='file:'+missing
    for role in affected:
        assert ids.index('validate:'+role)<ids.index('start:'+role)
    if missing.endswith('.service'):
        assert ids.index('systemd:reload')<ids.index('start:conductor')
    observations={step.step_id:'completed' for step in full}
    assert kuasar.install_recipe(roles=('conductor','proxy'),assets=a,state='partial',observed_steps=observations)==()
    observations['start:conductor']='stopped'
    repair=kuasar.install_recipe(roles=('conductor','proxy'),assets=a,state='partial',observed_steps=observations)
    assert len(repair)==1 and repair[0].argv==('systemctl','start','node-ctl.service')


def test_agentenv_partial_config_repair_reapplies_setup_and_restarts_server():
    a=assets('server','aenv','aenv-buildctl','uvm-ublk-daemon','runtime-assets','aenv.service','config.toml','aenv.env','api-key')
    full=agentenv.install_recipe(roles=('server',),assets=a,state='absent')
    observed={s.step_id:'completed' for s in full};observed['file:config.toml']='missing'
    repair=agentenv.install_recipe(roles=('server',),assets=a,state='partial',observed_steps=observed)
    ids=[s.step_id for s in repair]
    assert ids==['file:config.toml','ownership:config','setup:assets','setup:host','start:server']
    assert repair[-1].argv==('systemctl','restart','aenv.service')
    observed['setup:host']='unknown'
    with pytest.raises(ValueError):agentenv.install_recipe(roles=('server',),assets=a,state='partial',observed_steps=observed)


def test_agentenv_control_config_repair_activates_owned_container():
    a=assets('gateway.json','api-key')
    full=agentenv.install_recipe(roles=('gateway',),assets=a,state='absent',images={'gateway':'x@sha256:'+'a'*64})
    observed={s.step_id:'completed' for s in full};observed['file:gateway.json']='missing'
    repair=agentenv.install_recipe(roles=('gateway',),assets=a,state='partial',images={'gateway':'x@sha256:'+'a'*64},observed_steps=observed)
    assert [s.step_id for s in repair]==['file:gateway.json','container:gateway']
    assert repair[-1].kind=='owned-container'


@pytest.mark.parametrize('missing,unit',[
    ('config:CubeMaster','cube-sandbox-cubemaster.service'),
    ('CubeAPI','cube-sandbox-cube-api.service'),
    ('cubeproxy.compose','cube-sandbox-cube-proxy.service'),
    ('cube-sandbox-cubeops.service','cube-sandbox-cubeops.service'),
])
def test_cube_control_partial_repair_restarts_only_affected_service(missing,unit):
    from sandbox_graph.environment.products.probe import UNITS
    names=('CubeOps','CubeMaster','CubeAPI','CubeTemplateCenter')
    a=assets(*names,*('config:'+name for name in names),'cubeproxy.compose','dependency-lock',*UNITS['cubesandbox']['control'])
    kwargs=dict(assets=a,root='/opt/cube',config_paths={n:'/etc/cube/'+n+'.yaml' for n in names},proxy_compose_path='/etc/cube/proxy.yaml')
    full=cube.control_recipe(state='absent',**kwargs)
    observed={s.step_id:'completed' for s in full};observed['file:'+missing]='missing'
    repair=cube.control_recipe(state='partial',observed_steps=observed,**kwargs)
    assert [s.argv for s in repair if s.step_id.startswith('start:')]==[('systemctl','restart',unit)]
    if missing.endswith('.service'):
        assert [s.step_id for s in repair].index('systemd:reload')<[s.step_id for s in repair].index('start:'+unit)


@pytest.mark.parametrize('missing',['file:rendered-env','archive:oneclick','verify:package','verify:dependency-lock'])
def test_cube_oneclick_changed_inputs_cannot_silently_skip_installer(missing):
    a=assets('oneclick','package','dependency-lock','rendered-env')
    kwargs=dict(roles=('compute',),assets=a,root='/usr/local/services/cubetoolbox',config_path='/etc/cube.env')
    recipe=cube.install_recipe(state='absent',**kwargs)
    observed={step.step_id:'completed' for step in recipe};observed[missing]='missing'
    with pytest.raises(ValueError,match='original action reconciliation'):
        cube.install_recipe(state='partial',observed_steps=observed,**kwargs)
