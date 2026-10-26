"""BCT-16: literal input, independent expected conditions and execution binding."""
import json
from copy import deepcopy

import pytest

from sandbox_bench.casegen import generate_cases, load_case_file


def complete_case():
    return {'schema_version':'case.v1','method_id':'B01.TEMPLATE','method_version':'1',
        'product':'kuasar','product_version':'locked-test','work_id':'input/work','group_id':'G_1',
        'source':{'kind':'template','id':'template-1','digest':'sha256:template-1',
            'artifact_kind':'sandbox_artifact','launch_path':'cold_boot','producer_step':'pre_existing'},
        'spec':'2v2g','completion':{'endpoint':'STATE_READY','timeout_s':2.0,'poll_interval_s':0.01},
        'load':{'mode':'WINDOW','requests':2,'concurrency':1,'run_timeout_s':10.0,'max_unknown_external':0},
        'repeat_id':1,'topology':'node_direct','ttl_s':100.0,'cleanup_timeout_s':2.0,
        'cache_profile':'warm','environment_ref':'fixture:G_1','contract_ref':'contracthash',
        'graph_ref':'graph1','plan_ref':'input/plan','resource_observation_waived':True,
        'declared_metrics':[],'declared_conditions':[]}


def test_missing_required_field_is_not_supplied_from_default_case():
    raw=complete_case();del raw['completion']
    with pytest.raises(ValueError,match='completion'): generate_cases(raw,file_sha256='a'*64)


@pytest.mark.parametrize('field,value',[('surprise',1),('load',{'requests':2}),('spec','2x2g')])
def test_unknown_and_invalid_values_use_existing_contract_validation(field,value):
    raw=complete_case();raw[field]=value
    with pytest.raises(ValueError): generate_cases(raw,file_sha256='a'*64)


def test_single_complete_case_preserves_declared_values_and_source_hash():
    raw=complete_case();before=deepcopy(raw)
    item,=generate_cases(raw,file_sha256='a'*64)
    assert raw==before
    assert item.case.spec.vcpus==2 and item.case.spec.memory_bytes==2147483648
    assert item.case.load.requests==2 and item.case.load.concurrency==1
    assert item.case.product_version=='locked-test'
    assert item.provenance['file_sha256']=='a'*64
    assert item.provenance['conditions']['spec']=={'vcpus':2.0,'memory_bytes':2147483648,'disk_bytes':None}
    assert len(item.provenance['case_sha256'])==64


def test_axes_expand_stably_without_mutating_the_input():
    raw=complete_case();raw['axes']={'specs':['2v2g','2v4g'],'concurrency':[1,2],'requests':[3]}
    items=generate_cases(raw,file_sha256='a'*64)
    assert [(i.case.spec.memory_bytes,i.case.load.concurrency,i.case.load.requests) for i in items]==[
        (2147483648,1,3),(2147483648,2,3),(4294967296,1,3),(4294967296,2,3)]
    assert len({i.case.work_id for i in items})==4
    assert len({i.provenance['conditions_hash'] for i in items})==4
    assert len({i.template_ref for i in items})==4


@pytest.mark.parametrize('axes',[{'specs':[]},{'no_such_axis':[1]}, {'concurrency':[1,1]}])
def test_empty_unknown_and_duplicate_axes_rejected(axes):
    raw=complete_case();raw['axes']=axes
    with pytest.raises(ValueError): generate_cases(raw,file_sha256='a'*64)


def test_file_hash_is_for_original_bytes_and_duplicate_yaml_keys_are_rejected(tmp_path):
    import hashlib
    path=tmp_path/'case.json';path.write_text(json.dumps(complete_case()))
    item,=load_case_file(path)
    assert item.provenance['file_sha256']==hashlib.sha256(path.read_bytes()).hexdigest()
    path=tmp_path/'bad.yaml';path.write_text('spec: 2v2g\nspec: 2v4g\n')
    with pytest.raises(ValueError,match='duplicate'): load_case_file(path)


def test_benchmark_loader_uses_complete_file_and_rejects_implicit_matrix_selection(tmp_path):
    from sandbox_bench.runner import load_case
    path=tmp_path/'case.json';path.write_text(json.dumps(complete_case()))
    assert load_case(path).spec.memory_bytes==2147483648
    raw=complete_case();raw['axes']={'concurrency':[1,2]};path.write_text(json.dumps(raw))
    with pytest.raises(ValueError,match='matrix'): load_case(path)


def test_graph_template_freezes_case_and_provenance():
    item,=generate_cases(complete_case(),file_sha256='a'*64)
    planning,template=item.register(config={'api_url':'http://localhost:1'},priority='base',
        capability='supported',adapter_verified=True,conflict_domains=('node-1',))
    assert planning.method_id=='B01.TEMPLATE'
    assert planning.conditions==({'case_template_ref':item.template_ref},)
    assert template['case']['load']['requests']==2
    assert template['config']['case_input_provenance']==item.provenance


@pytest.mark.asyncio
async def test_generated_file_drives_real_graph_sessions_and_evidence(tmp_path):
    from scripts.mock_pipeline import run_mock
    path=tmp_path/'case.json';raw=complete_case()
    raw['axes']={'specs':['2v2g','2v4g'],'requests':[3]}
    path.write_text(json.dumps(raw))
    summary=await run_mock(case_files=[path],store_root=tmp_path/'store',out=tmp_path/'out')
    cases=[json.loads(p.read_text()) for p in (tmp_path/'store'/'sessions').glob('*/case.json')]
    assert len(cases)==2
    assert sorted(c['spec']['memory_bytes'] for c in cases)==[2147483648,4294967296]
    assert all(c['load']['requests']==3 and c['load']['concurrency']==1 for c in cases)
    manifests=list((tmp_path/'store'/'sessions').glob('*/evidence/case_manifest.json'))
    assert len(manifests)==2
    comparison=[]
    for manifest in manifests:
        data=json.loads(manifest.read_text())['metadata']
        assert data['case_input_provenance']['file_sha256']==__import__('hashlib').sha256(path.read_bytes()).hexdigest()
        comparison.append(data['comparison']['case_input']['conditions_hash'])
    assert len(set(comparison))==2


def test_generate_cli_writes_explicit_cells_and_provenance(tmp_path,capsys):
    from sandbox_bench.runner import main
    path=tmp_path/'input.json';raw=complete_case();raw['axes']={'concurrency':[1,2]}
    path.write_text(json.dumps(raw))
    assert main(['generate','--case',str(path),'--output',str(tmp_path/'cells')])==0
    assert len(list((tmp_path/'cells').glob('case-*.input.json')))==2
    assert json.loads((tmp_path/'cells'/'case-0001.json').read_text())['load']['concurrency']==2


def test_provenance_mismatch_is_rejected_before_creating_session(tmp_path):
    from sandbox_bench.session import BenchSession
    item,=generate_cases(complete_case(),file_sha256='a'*64)
    case=item.case.model_copy(update={'load':item.case.load.model_copy(update={'requests':99})})
    with pytest.raises(ValueError,match='provenance'):
        BenchSession(case,{'case_input_provenance':item.provenance},tmp_path/'session')
    assert not (tmp_path/'session').exists()


def test_repository_complete_profiles_are_usable():
    from pathlib import Path
    root=Path(__file__).resolve().parents[3]
    one=load_case_file(root/'config/case-low.yaml')
    matrix=load_case_file(root/'config/case-matrix.yaml')
    assert len(one)==1 and len(matrix)==4
    assert all(i.case.load.requests==2 for i in (*one,*matrix))
