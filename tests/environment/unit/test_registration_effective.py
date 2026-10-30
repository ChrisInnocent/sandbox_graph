"""Runtime evidence must describe the current node, not a matching old value."""
from datetime import datetime, timezone
import json
from types import SimpleNamespace

import pytest

from sandbox_graph.environment.catalog import audited_sources
from sandbox_graph.environment.contracts import TargetPolicy, TargetValue
from sandbox_graph.environment.probes.registration import RuntimeRegistrationProbe, interpret_registration
from tests.environment.unit.test_planner import declaration


def kuasar_record(**updates):
    node = dict(node_id='n1', last_heartbeat_unix=datetime.now(timezone.utc).timestamp(),
                link_owner='registry-1', capacity=20,
                build_execution_capacity={'max_builds':4},
                build_registration_capacity={'max_builds':99})
    node.update(updates)
    return {'ok':True, 'found':True, 'node':node}


async def check(raw, *, product='kuasar', name='max_builds', value=4, mode='exact', unit='count', source=True, roles=None, integrated=False):
    target = TargetValue(mode=mode, **({'value':value, 'unit':unit} if mode=='exact' else {}))
    d = declaration().model_copy(update={
        'product':product, 'profile':'kuasar.native.v1' if product=='kuasar' else 'cube.oneclick.v1',
        'source_set':('orchestrator='+audited_sources()['orchestrator'],) if source else (),
        'service_endpoints':{'manager':'http://manager'},
        'targets':TargetPolicy(platform_limits={name:target})})
    node = d.nodes[0].model_copy(update={'roles':roles or (('conductor',) if product=='kuasar' else ('compute',))})
    calls=[]; evidence=[]
    class Probe:
        def commit(self, data):
            evidence.append(data)
            return 'artifact://check/'+('a'*64)
        async def request(self,node,action,payload):
            assert action=='probe.configuration.v1'
            return {'components':{'conductor':{'status':'satisfied','evidence_level':'startup_bound',
                'values':{'builder.admission.execution.max_builds':4}}}}
    def http(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return SimpleNamespace(status=200, body=json.dumps(raw).encode())
    runtime=RuntimeRegistrationProbe(probe=Probe(), bindings={
        'n1':{'endpoint_ref':'manager', 'product_node_id':'n1'}}, http=http)
    if integrated:
        from sandbox_graph.environment.verification import ProductVerification
        rows=await ProductVerification(probe=Probe(),scope='test',store=None,runtime=runtime).check(
            d,node,fresh=True,boot_id='boot')
    else:rows=await runtime.check(d,node,boot_id='boot')
    return next(row for row in rows if row.check_id.endswith(':'+name)), calls, evidence


@pytest.mark.asyncio
async def test_execution_capacity_reads_execution_not_registration_and_filters_secrets():
    raw=kuasar_record(key_pairs=[{'secret':'do-not-export'}])
    row,calls,evidence=await check(raw)
    assert row.status=='satisfied' and row.observed['actual']==4
    assert row.observed['evidence_level']=='runtime_effective'
    assert row.observed['source_field']=='node.build_execution_capacity.max_builds'
    assert calls[0][0]=='POST' and json.loads(calls[0][2]['body'])=={'op':'runtime','node_id':'n1'}
    assert 'do-not-export' not in str(evidence)


@pytest.mark.asyncio
@pytest.mark.parametrize('patch',[
    {'last_heartbeat_unix':1}, {'last_heartbeat_unix':1e20},
    {'node_id':'another-node'}, {'link_owner':''}, {'draining':True},
])
async def test_unready_registration_never_attests_matching_parameter(patch):
    row,_,_=await check(kuasar_record(**patch), name='sandbox_capacity', value=20)
    assert row.status=='unknown' and row.repair_code is None
    assert row.observed['evidence_level']=='unavailable'


@pytest.mark.asyncio
async def test_stale_cube_value_cannot_pass_or_request_parameter_repair():
    raw={'uuid':'n1','Healthy':True,'ReportedReady':True,
         'MetaDataUpdateAt':'2000-01-01T00:00:00Z','MaxMvmLimit':20}
    row,_,_=await check(raw, product='cubesandbox',name='max_sandboxes',value=20)
    assert row.status=='unknown' and row.repair_code is None


@pytest.mark.asyncio
@pytest.mark.parametrize('value',[True, '4', -1, 4.5, None, 2**63])
async def test_malformed_execution_limit_is_not_effective(value):
    row,_,_=await check(kuasar_record(build_execution_capacity={'max_builds':value}), mode='preserve')
    assert row.status=='unknown'


@pytest.mark.asyncio
@pytest.mark.parametrize('value',[None, [], 'invalid'])
async def test_missing_execution_object_is_not_unlimited(value):
    row,_,_=await check(kuasar_record(build_execution_capacity=value), mode='preserve')
    assert row.status=='unknown'


@pytest.mark.asyncio
async def test_unpinned_source_does_not_interpret_new_wire_contract():
    row,_,_=await check(kuasar_record(),source=False)
    assert row.status=='unknown' and row.observed['reason']=='audited_source_binding_required'


@pytest.mark.asyncio
async def test_runtime_role_alone_does_not_claim_conductor_capacity():
    row,calls,_=await check(kuasar_record(),roles=('runtime',))
    assert row.status=='not_applicable'


@pytest.mark.asyncio
async def test_incompatible_unit_is_unknown_instead_of_crashing_check():
    row,_,_=await check(kuasar_record(),name='sandbox_capacity',value=20,unit='MiB')
    assert row.status=='unknown' and row.repair_code is None


@pytest.mark.asyncio
@pytest.mark.parametrize('field',['sandbox_capacity','max_builds'])
async def test_go_omitempty_zero_is_explicit_unlimited_in_audited_schema(field):
    raw=kuasar_record(build_execution_capacity={})
    del raw['node']['capacity']
    row,_,_=await check(raw,name=field,value=0)
    assert row.status=='satisfied' and row.observed['actual']==0
    assert row.observed['zero_semantics']=='unlimited'


@pytest.mark.asyncio
@pytest.mark.parametrize('value',[True, '20', -1, 20.5, None])
async def test_invalid_capacity_cannot_be_preserved(value):
    row,_,_=await check(kuasar_record(capacity=value),name='sandbox_capacity',mode='preserve')
    assert row.status=='unknown'


def test_naive_timestamp_is_not_interpreted_in_controller_local_timezone():
    raw={'uuid':'n1','Healthy':True,'ReportedReady':True,
         'MetaDataUpdateAt':'2026-10-26T00:00:00','MaxMvmLimit':20}
    now=datetime(2026,10,26,tzinfo=timezone.utc).timestamp()
    assert interpret_registration('cubesandbox',raw,node_id='n1',now=now)['status']=='mismatch'


@pytest.mark.asyncio
@pytest.mark.parametrize('runtime_value,expected',[(4,'satisfied'),(3,'mismatch'),(None,'unknown')])
async def test_verifier_keeps_matching_startup_file_separate_from_runtime(runtime_value,expected):
    raw=kuasar_record(build_execution_capacity={'max_builds':runtime_value})
    row,_,_=await check(raw,integrated=True)
    assert row.status==expected
    assert row.repair_code==('CONFIGURATION_MISMATCH' if expected=='mismatch' else None)


@pytest.mark.asyncio
async def test_wrong_source_commit_is_not_a_supported_wire_version():
    from unittest.mock import patch
    # Input declaration uses the audited source, while the probe is now bound
    # to another source profile. A matching number must not bridge versions.
    with patch('sandbox_graph.environment.catalog.audited_sources',return_value={'orchestrator':'0'*40}):
        row,_,_=await check(kuasar_record())
    assert row.status=='unknown'
