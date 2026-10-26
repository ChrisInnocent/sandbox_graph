import pytest
from sandbox_graph.environment.probes.registration import interpret_registration


def test_cube_requires_fresh_reported_ready_and_healthy():
    raw={'uuid':'n1','Healthy':True,'ReportedReady':True,'MetaDataUpdateAt':'2026-10-24T00:00:00Z','MaxMvmLimit':100}
    from datetime import datetime,timezone
    now=datetime(2026,10,24,tzinfo=timezone.utc).timestamp()
    assert interpret_registration('cubesandbox',raw,node_id='n1',now=now)['status']=='satisfied'
    assert interpret_registration('cubesandbox',raw,node_id='n1',now=now+121)['status']=='mismatch'
    raw['Healthy']=False
    assert interpret_registration('cubesandbox',raw,node_id='n1',now=now)['status']=='mismatch'


def test_kuasar_runtime_is_not_just_an_existing_node_and_never_exposes_keys():
    raw={'ok':True,'found':True,'node':{'node_id':'n1','last_heartbeat_unix':1000,'link_owner':'registry-1',
        'capacity':20,'api_endpoint':'http://n1','key_pairs':[{'secret':'private'}],'resume_token':'private'}}
    result=interpret_registration('kuasar',raw,node_id='n1',now=1001)
    assert result['status']=='satisfied' and result['effective']['sandbox_capacity']==20
    assert 'private' not in str(result)
    assert interpret_registration('kuasar',raw,node_id='other',now=1001)['status']=='mismatch'
    assert interpret_registration('kuasar',raw,node_id='n1',now=1300)['status']=='mismatch'


def test_agentenv_requires_service_instance_and_ready_observed_status():
    raw={'id':'n1','serviceInstanceID':'instance','status':'ready','metrics':{'CPUCount':4,'memoryTotalBytes':4096}}
    assert interpret_registration('agentenv',raw,node_id='n1',now=1)['status']=='satisfied'
    raw['status']='connecting'
    assert interpret_registration('agentenv',raw,node_id='n1',now=1)['status']=='mismatch'

@pytest.mark.asyncio
async def test_effective_cube_memory_converts_mib_to_declared_gib_and_checks_http_status():
    from types import SimpleNamespace
    import json
    from datetime import datetime,timezone
    from sandbox_graph.environment.probes.registration import RuntimeRegistrationProbe
    from sandbox_graph.environment.contracts import TargetPolicy,TargetValue
    from tests.environment.unit.test_planner import declaration
    d=declaration().model_copy(update={'service_endpoints':{'master':'http://master'},
        'targets':TargetPolicy(platform_limits={'quota_memory':TargetValue(mode='exact',value=4,unit='GiB')})})
    class Probe:
        def commit(self,value):return 'artifact://check/'+('a'*64)
    response=SimpleNamespace(status=200,body=json.dumps({'uuid':'n1','Healthy':True,'ReportedReady':True,
        'MetaDataUpdateAt':datetime.now(timezone.utc).isoformat(),'QuotaMem':4096}).encode())
    p=RuntimeRegistrationProbe(probe=Probe(),bindings={'n1':{'endpoint_ref':'master','product_node_id':'n1'}},http=lambda *a,**k:response)
    rows=await p.check(d,d.nodes[0],boot_id='boot')
    assert next(x for x in rows if x.check_id.endswith(':quota_memory')).status=='satisfied'
    response.status=403
    assert (await p.check(d,d.nodes[0],boot_id='boot'))[0].status!='satisfied'


@pytest.mark.asyncio
async def test_manager_is_not_looked_up_as_a_compute_registration():
    from sandbox_graph.environment.probes.registration import RuntimeRegistrationProbe
    from tests.environment.unit.test_planner import declaration
    from sandbox_graph.environment.contracts import TargetPolicy,TargetValue
    d=declaration().model_copy(update={'product':'kuasar','targets':TargetPolicy(platform_limits={
        'sandbox_capacity':TargetValue(mode='exact',value=20,unit='count')})})
    node=d.nodes[0].model_copy(update={'roles':('registry',)})
    class Probe:
        def commit(self,value):return 'artifact://check/'+('a'*64)
    def forbidden(*args,**kwargs):raise AssertionError('manager is not a compute node')
    rows=await RuntimeRegistrationProbe(probe=Probe(),bindings={},http=forbidden).check(d,node,boot_id='boot')
    assert rows and all(row.status=='not_applicable' for row in rows)


@pytest.mark.asyncio
async def test_management_probe_checks_actual_boot_installation_and_startup_config(tmp_path):
    from sandbox_graph.environment.probes.environment import RemoteEnvironmentProbe
    from types import SimpleNamespace
    from tests.environment.unit.test_planner import declaration
    d=declaration();node=d.nodes[0].model_copy(update={'roles':('registry',)})
    class Access:
        async def check(self,node):return SimpleNamespace(identity_status='confirmed',execution_status='allowed',observation={'boot_id':'boot'})
    p=RemoteEnvironmentProbe(transport=None,resolver=None,store=None,scope='test',access=Access())
    installation={'installation':'installed','services':[{'unit':'registry','status':'running','pid':10}]}
    async def request(node,action,payload):
        return installation if action=='probe.product.v1' else {'components':{'registry':{'status':'satisfied','pid':10}}}
    p.request=request
    proof=await p.management_dependency(d,node,'boot')
    assert proof['status']=='verified' and proof['evidence_level']=='group_dependency'
    assert (await p.management_dependency(d,node,'another-boot'))['status']=='unavailable'
    installation['installation']='partial'
    assert (await p.management_dependency(d,node,'boot'))['status']=='unavailable'


@pytest.mark.asyncio
async def test_known_unexposed_scheduler_limit_is_capability_gap_not_install_repair():
    from sandbox_graph.environment.probes.registration import RuntimeRegistrationProbe
    from tests.environment.unit.test_planner import declaration
    from sandbox_graph.environment.contracts import TargetPolicy,TargetValue
    d=declaration().model_copy(update={'product':'agentenv','targets':TargetPolicy(platform_limits={
        'max_sandbox_count':TargetValue(mode='exact',value=100,unit='count')})})
    node=d.nodes[0].model_copy(update={'roles':('scheduler',)})
    class Probe:
        def commit(self,value):return 'artifact://check/'+('a'*64)
    rows=await RuntimeRegistrationProbe(probe=Probe(),bindings={}).check(d,node,boot_id='boot')
    row=next(r for r in rows if r.check_id.endswith(':max_sandbox_count'))
    assert row.status=='unsupported' and row.repair_code is None
    assert row.observed['capability']['components'][0]['source'].endswith('metrics.go')


def test_parameter_capability_catalog_covers_rules_with_scoped_source_refs():
    from pathlib import Path
    from sandbox_graph.environment.probes.parameter_capabilities import catalog
    from sandbox_graph.environment.tuning import RULES
    rows=catalog();root=Path(__file__).resolve().parents[3]
    assert {(r['product'],r['component'],r['parameter']) for r in rows}=={
        (p,c,n) for (p,c),rules in RULES.items() for n in rules}
    assert all(not Path(r['source']).is_absolute() and '..' not in Path(r['source']).parts and r['source_scope'] for r in rows)
