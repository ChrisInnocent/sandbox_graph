import importlib
import importlib.util
import pytest


def module():
    assert importlib.util.find_spec('sandbox_bench.contracts') is not None, 'contracts implementation missing'
    return importlib.import_module('sandbox_bench.contracts')


def sample():
    return dict(schema_version='case.v1', method_id='B01.TEMPLATE', method_version='1',
                product='kuasar', product_version='source-sha', work_id='w', group_id='g',
                source={'kind':'template','id':'e2b-snp-x','digest':'sha256:fixture',
                        'artifact_kind':'sandbox_artifact','launch_path':'cold_boot','producer_step':'pre_existing'},
                spec={'vcpus':2,'memory_bytes':2147483648},
                completion={'endpoint':'STATE_READY','timeout_s':5,'poll_interval_s':.1},
                load={'mode':'WINDOW','requests':5,'concurrency':2,'run_timeout_s':20,
                      'max_unknown_external':2},
                cleanup_timeout_s=5, ttl_s=100,
                cache_profile='declared-warm', environment_ref='envhash',
                contract_ref='contracthash', graph_ref='graph1', plan_ref='plan1')


def test_contract_requires_units_and_preserves_hash():
    m=module(); data=sample(); c=m.CaseContract.model_validate(data)
    assert c.spec.memory_bytes==2147483648
    assert c.digest()==m.CaseContract.model_validate(dict(reversed(list(data.items())))).digest()
    for mutate in [lambda x:x.update(garbage=1),lambda x:x['spec'].update(memory_bytes=True),
                   lambda x:x['completion'].update(timeout_s=float('nan')),
                   lambda x:x['load'].update(requests=0),lambda x:x.update(ttl_s=1)]:
        data=sample();mutate(data)
        with pytest.raises(ValueError):m.CaseContract.model_validate(data)


def test_method_specific_contracts_reject_before_execution():
    m=module(); data=sample();data['method_id']='B10.SLO_VERIFY'
    with pytest.raises(ValueError):m.CaseContract.model_validate(data)
    data=sample();data['method_id']='B10.FIXED_REQUESTS.BATCH'
    with pytest.raises(ValueError):m.CaseContract.model_validate(data)
    data=sample();data['method_id']='B05.NATIVE';data['clone_count']=3
    with pytest.raises(ValueError):m.CaseContract.model_validate(data)


def test_registry_has_methods_and_does_not_claim_verified():
    assert importlib.util.find_spec('sandbox_bench.registry') is not None, 'registry missing'
    from sandbox_bench.registry import METHODS, capability
    assert 'B10.FIXED_REQUESTS.WINDOW' in METHODS
    assert all(f'E{i:02}' in {x.split('.')[0] for x in METHODS} for i in range(1,7))
    assert capability('kuasar','B02.FULL')['environment_verified'] is False
    assert capability('unknown','B01.TEMPLATE')['status']=='unverified'

@pytest.mark.parametrize('method,endpoint',[
    ('B03.PAUSE','EXEC_COMPLETE'),('B06.RUNNING','EXEC_COMPLETE'),
    ('B07.FIRST','STATE_READY'),('B08.LOCAL','STATE_READY'),
    ('B08.PUBLISHED','API_COMPLETE')])
def test_method_completion_matrix_rejects_mislabeled_or_waking_endpoints(method,endpoint):
    data=sample();data['method_id']=method
    data['completion'].update(endpoint=endpoint,command='true',expected_stdout='')
    with pytest.raises(ValueError,match='method completion'):
        module().CaseContract.model_validate(data)

def test_paused_and_deleted_targets_cannot_have_command_postvalidation():
    data=sample();data['method_id']='B03.PAUSE'
    data['completion'].update(validation_policy='stream_after',command='true',expected_stdout='')
    with pytest.raises(ValueError,match='non-waking'):
        module().CaseContract.model_validate(data)

def test_repeat_identity_is_positive_and_changes_case_binding():
    first=module().CaseContract.model_validate(sample())
    second=module().CaseContract.model_validate({**sample(),'repeat_id':2})
    assert first.repeat_id==1 and second.repeat_id==2
    assert first.digest()!=second.digest()
    with pytest.raises(ValueError):
        module().CaseContract.model_validate({**sample(),'repeat_id':True})

def test_event_and_resource_contracts_reject_ambiguous_units_and_epochs():
    m=module()
    event=dict(schema_version='event.v1',event_id='e',seq=1,work_id='w',operation_id='o',
               stage='create',event_kind='completed',clock_domain='node:boot:process',
               process_epoch='process',monotonic_ns=1,observed_at_ns=1,payload_ref='sha256:a',validity='valid')
    assert m.EventEnvelope.model_validate(event).seq==1
    for mutate in (lambda x:x.update(seq=True),lambda x:x.update(process_epoch=''),lambda x:x.update(monotonic_ns=-1)):
        value=event.copy();mutate(value)
        with pytest.raises(ValueError):m.EventEnvelope.model_validate(value)
    identity=dict(environment='e',node='n',boot_id='b',pid=42,start_ticks=10)
    resource=dict(schema_version='resource.v1',identity=identity,roles=('data',),components=('vmm',),
                  scope='process',clock_domain='n:b:p',process_epoch='p',observed_at_ns=1,
                  pss_bytes=100,cpu_seconds=1,status='valid')
    assert m.ResourceSample.model_validate(resource).pss_bytes==100
    resource['pss_bytes']=True
    with pytest.raises(ValueError):m.ResourceSample.model_validate(resource)
