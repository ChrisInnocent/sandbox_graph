import importlib
import pytest
from tests.benchmark.unit.test_contract_models import sample
from sandbox_bench.contracts import CaseContract
from sandbox_bench.load import OperationResult

class Scenario:
    def __init__(self):self.calls=[]
    async def execute(self,index):
        self.calls.append(index)
        return OperationResult('success',value={'operation_id':str(index),'elapsed_s':.1,'status':'success'})
    async def setup(self,**kwargs):return {'status':'ready'}
    async def validate(self):return {'status':'valid'}

@pytest.mark.asyncio
async def test_step_receipts_replay_no_new_calls(tmp_path):
    from sandbox_bench.session import BenchSession
    case=CaseContract.model_validate(sample());scenario=Scenario()
    session=BenchSession(case,{'api_url':'http://localhost:1','data_url':'http://localhost:1','product':'kuasar'},tmp_path,scenario=scenario)
    result=await session.apply_step('N05','frozen-input')
    assert result['status']=='completed'
    assert scenario.calls==[0,1,2,3,4]
    assert await session.apply_step('N05','frozen-input')==result
    assert scenario.calls==[0,1,2,3,4]
    with pytest.raises(ValueError):await session.apply_step('N05','other-input')

@pytest.mark.asyncio
async def test_unreceipted_submission_reconciles_without_replay(tmp_path):
    from sandbox_bench.session import BenchSession
    case=CaseContract.model_validate(sample());scenario=Scenario()
    session=BenchSession(case,{'api_url':'http://localhost:1','data_url':'http://localhost:1','product':'kuasar'},tmp_path,scenario=scenario)
    token=session.journal.reserve_sync(0);session.journal.submission_started_sync(token)
    session.journal.begin_step('N05','h')
    result=await session.apply_step('N05','h')
    assert result['status']=='unknown'
    assert scenario.calls==[]

@pytest.mark.asyncio
async def test_cancel_persisted_and_wrong_scope_rejected(tmp_path):
    from sandbox_bench.session import BenchSession
    case=CaseContract.model_validate(sample());scenario=Scenario()
    config={'api_url':'http://localhost:1','data_url':'http://localhost:1','product':'kuasar'}
    session=BenchSession(case,config,tmp_path,scenario=scenario)
    await session.cancel('requested')
    assert (await session.apply_step('N05','h'))['status']=='cancelled'
    assert scenario.calls==[]
    with pytest.raises(ValueError):BenchSession(case,{**config,'api_url':'http://other'},tmp_path,scenario=scenario)

@pytest.mark.asyncio
async def test_analysis_and_seal_recompute_same_measured_events(tmp_path):
    from sandbox_bench.session import BenchSession
    from sandbox_bench.analysis import recompute_case
    case=CaseContract.model_validate(sample())
    session=BenchSession(case,{'api_url':'http://localhost:1','data_url':'http://localhost:1','product':'kuasar'},tmp_path,scenario=Scenario())
    assert (await session.apply_step('N05','h'))['successes']==5
    analysis=await session.apply_step('N08','a')
    assert analysis['status']=='completed'
    assert analysis['analysis']['throughput']['count']==5
    assert (await session.apply_step('N09','c'))['status']=='completed'
    sealed=await session.apply_step('N10','s')
    assert sealed['status']=='completed'
    assert recompute_case(tmp_path/'evidence')['throughput']['count']==5

def test_partial_clone_keeps_successful_child_and_rejects_resource_sequence_gap(tmp_path):
    from sandbox_bench.session import BenchSession
    from sandbox_bench.runner import atomic_json
    from sandbox_bench.analysis import analyze_case
    data=sample();data.update(method_id='B05.NATIVE',clone_count=2);data['load']['requests']=4
    session=BenchSession(CaseContract.model_validate(data),{'api_url':'http://localhost:1','data_url':'http://localhost:1','product':'kuasar'},tmp_path,scenario=Scenario())
    atomic_json(tmp_path/'load.json',{'started_at':0,'finished_at':2,'clock_domain':'c','process_epoch':'p',
        'events':[],'results':{'0':{'status':'failed','value':{'completion':{'observed_at':2},'children':[
            {'status':'success','completion':{'latency':1,'observed_at':1}}, {'status':'failed'}]}}}})
    records=session.records()
    assert len(records['operations'])==2 and len(records['parent_operations'])==1
    assert analyze_case(records)['throughput']['count']==1
    atomic_json(tmp_path/'resource_samples/one.json',{'seq':1,'kind':'sample_gap','through_seq':1})
    atomic_json(tmp_path/'resource_samples/three.json',{'seq':3,'kind':'sample_gap','through_seq':3})
    with pytest.raises(ValueError,match='sequence'):session.records()

@pytest.mark.asyncio
async def test_preflight_rejects_different_request_ttl(tmp_path):
    from sandbox_bench.session import BenchSession
    case=CaseContract.model_validate(sample())
    config={'product':'kuasar','api_url':'http://localhost:1','data_url':'http://localhost:1',
            'source':{'kind':case.source.kind,'id':case.source.id},'create_body':{'timeout':1},
            'environment_evidence':{'ref':case.environment_ref,'product_version':case.product_version}}
    session=BenchSession(case,config,tmp_path,scenario=Scenario())
    result=await session.apply_step('N01','h')
    assert result['status']=='blocked' and 'ttl' in result['reason']

@pytest.mark.asyncio
async def test_stop_keeps_unacknowledged_spool_recoverable(tmp_path):
    from sandbox_bench.session import BenchSession
    class Sampler:
        clock_domain='domain'
        closed=False
        connected=False
        def reconcile(self,sink):
            return {'status':'reconciled' if self.connected else 'disconnected','pending':0 if self.connected else 2}
        def close(self):self.closed=True
    sampler=Sampler()
    session=BenchSession(CaseContract.model_validate(sample()),
        {'api_url':'http://localhost:1','data_url':'http://localhost:1','product':'kuasar'},tmp_path,
        scenario=Scenario(),sampler=sampler,clock_domain='domain')
    result=await session._N07()
    assert result['status']=='unknown' and result['pending_samples']==2
    assert not sampler.closed
    assert (await session.reconcile('N07'))['status']=='unknown'
    assert 'observation_evidence_pending' in session.records()['evidence_gaps']
    restored=BenchSession(CaseContract.model_validate(sample()),
        {'api_url':'http://localhost:1','data_url':'http://localhost:1','product':'kuasar'},tmp_path,scenario=Scenario())
    assert (await restored._N07())['status']=='unknown'
    sampler.connected=True
    assert (await session._N07())['status']=='completed'
    assert sampler.closed
    assert (await session._N07())['status']=='completed'

class ValidationAdapter:
    def __init__(self):self.executed=[]
    def info(self,handle,timeout):return {'state':'running'}
    def execute(self,handle,command,timeout):
        self.executed.append(handle['sandbox_id'])
        return {'stdout':'ok','exit_code':0}
    def effective_spec(self,handle,timeout):return {'vcpus':2,'memory_mib':2048}
    def delete(self,handle,timeout):return None

@pytest.mark.asyncio
async def test_partial_clone_validates_each_success_child_and_retains_source_recovery_gap(tmp_path):
    from sandbox_bench.session import BenchSession
    from sandbox_bench.runner import atomic_json
    data=sample();data.update(method_id='B05.NATIVE',clone_count=2,source_must_run=True)
    data['load']['requests']=4
    data['completion'].update(validation_policy='batch_after',command='echo ok',expected_stdout='ok')
    session=BenchSession(CaseContract.model_validate(data),
        {'api_url':'http://localhost:1','data_url':'http://localhost:1','product':'kuasar'},tmp_path,scenario=Scenario())
    adapter=ValidationAdapter();session.adapter=adapter
    for sid in ('c1','c2'):session.journal.own('sandbox',sid,{'environment':session.environment})
    atomic_json(tmp_path/'load.json',{'started_at':0,'finished_at':2,'clock_domain':'c','events':[],
        'results':{'0':{'status':'failed','value':{'validation_interference':True,
            'source_recovery':{'status':'unknown','reason':'response_lost'},
            'source_residual':{'sandbox_id':'source','status':'unknown'},
            'completion':{'observed_at':2},'children':[
                {'sandbox_id':sid,'status':'success','validation':{'success':True},
                 'completion':{'latency':1,'observed_at':1}} for sid in ('c1','c2')]}}}})
    result=await session._N08()
    assert adapter.executed==['c1','c2']
    assert result['data_status']=='unverified'
    assert result['analysis']['quality']['completed_success_count']==2
    assert result['analysis']['qualifications']['notes'][0]['kind']=='validation_interference'
    assert 'source_recovery:0:unknown' in result['gaps']
    assert (await session.reconcile())['status']=='unknown'
    assert (await session._N09())['status']=='incomplete'
    await session._N08()
    assert adapter.executed==['c1','c2']

@pytest.mark.asyncio
async def test_noncreate_batch_validation_recovers_only_persisted_target_binding(tmp_path):
    from sandbox_bench.session import BenchSession
    from sandbox_bench.runner import atomic_json
    data=sample();data['method_id']='B04.DISK'
    data['completion'].update(validation_policy='batch_after',command='echo ok',expected_stdout='ok')
    config={'api_url':'http://localhost:1','data_url':'http://localhost:1','product':'kuasar',
            'targets':[{'sandbox_id':'prepared','generation':'g1'}],
            'fixture_input':{'environment_ref':data['environment_ref'],'evidence_ref':'fixture'}}
    session=BenchSession(CaseContract.model_validate(data),config,tmp_path,scenario=Scenario())
    await session._N02()
    atomic_json(tmp_path/'load.json',{'started_at':0,'finished_at':2,'clock_domain':'c','events':[],
        'results':{'0':{'status':'success','value':{'completion':{'latency':1,'observed_at':1},
                                                'validation':{'success':True}}}}})
    restored=BenchSession(CaseContract.model_validate(data),config,tmp_path,scenario=Scenario())
    adapter=ValidationAdapter();restored.adapter=adapter
    result=await restored._N08()
    assert adapter.executed==['prepared'] and result['data_status']=='valid'
    (tmp_path/'target_bindings.json').unlink()
    assert (await restored._validate_commands())[0]['status']=='unverified'
    assert adapter.executed==['prepared']

@pytest.mark.asyncio
async def test_seal_reuse_rejects_late_cleanup_instead_of_silently_omitting_it(tmp_path):
    from sandbox_bench.session import BenchSession
    session=BenchSession(CaseContract.model_validate(sample()),
        {'api_url':'http://localhost:1','data_url':'http://localhost:1','product':'kuasar'},tmp_path,scenario=Scenario())
    assert (await session.apply_step('N10','seal'))['status']=='completed'
    assert (await session._N10())['status']=='completed'
    with session.journal._transaction() as state:
        state['cleanup']=[{'resource_id':'late','status':'unknown'}]
    with pytest.raises(ValueError,match='seal.*changed|changed.*seal'):
        await session._N10()
    with pytest.raises(ValueError,match='seal.*changed|changed.*seal'):
        await session.apply_step('N10','seal')

@pytest.mark.asyncio
async def test_unreceipted_source_stage_prevents_false_reconciled_cleanup(tmp_path):
    from sandbox_bench.session import BenchSession
    session=BenchSession(CaseContract.model_validate(sample()),
        {'api_url':'http://localhost:1','data_url':'http://localhost:1','product':'kuasar'},tmp_path,scenario=Scenario())
    session.journal.stage_event({'request_index':0,'stage':'source_resume','child_index':None,
                                'phase':'intent','compensation':True},max_submissions=1)
    assert (await session.reconcile())['status']=='unknown'
    assert (await session._N09())['status']=='incomplete'

@pytest.mark.asyncio
async def test_recovery_observation_survives_discarded_parent_result(tmp_path):
    from sandbox_bench.session import BenchSession
    session=BenchSession(CaseContract.model_validate(sample()),
        {'api_url':'http://localhost:1','data_url':'http://localhost:1','product':'kuasar'},tmp_path,scenario=Scenario())
    session.journal.stage_event({'request_index':0,'stage':'source_resume','child_index':None,
        'phase':'observed','compensation':True,'sandbox_id':'source',
        'completion':{'status':'timeout','reason':'source_still_paused'}},max_submissions=1)
    assert (await session.reconcile())['status']=='unknown'
    assert 'source_recovery:0:timeout' in session.records()['evidence_gaps']

@pytest.mark.asyncio
async def test_subset_cleanup_preserves_prior_resource_observations(tmp_path):
    from sandbox_bench.session import BenchSession
    session=BenchSession(CaseContract.model_validate(sample()),
        {'api_url':'http://localhost:1','data_url':'http://localhost:1','product':'kuasar'},tmp_path,scenario=Scenario())
    session.adapter=ValidationAdapter()
    for sid in ('a','b'):session.journal.own('sandbox',sid,{'environment':session.environment})
    await session._N09({'sandbox:a'})
    await session._N09({'sandbox:b'})
    assert [row['resource_id'] for row in session.records()['cleanup_ledger']]==['a','b']

@pytest.mark.asyncio
async def test_late_recovery_gap_changes_qualification_before_seal(tmp_path):
    from sandbox_bench.session import BenchSession
    from sandbox_bench.runner import atomic_json
    from sandbox_bench.analysis import analyze_case
    session=BenchSession(CaseContract.model_validate(sample()),
        {'api_url':'http://localhost:1','data_url':'http://localhost:1','product':'kuasar'},tmp_path,scenario=Scenario())
    session.adapter=ValidationAdapter()
    session.journal.own('sandbox','s',{'environment':session.environment})
    atomic_json(tmp_path/'load.json',{'started_at':0,'finished_at':2,'clock_domain':'c','events':[],
        'results':{'0':{'status':'success','value':{'sandbox_id':'s','completion':{'latency':1,'observed_at':1}}}}})
    assert (await session._N08())['data_status']=='valid'
    session.journal.stage_event({'request_index':0,'stage':'source_resume','child_index':None,
        'phase':'observed','compensation':True,'sandbox_id':'source',
        'completion':{'status':'timeout'}},max_submissions=1)
    assert analyze_case(session.records())['qualifications']['status']=='unverified'

@pytest.mark.asyncio
async def test_seal_retains_execution_journal_and_rejects_late_owned_resource(tmp_path):
    import json
    from sandbox_bench.session import BenchSession
    session=BenchSession(CaseContract.model_validate(sample()),
        {'api_url':'http://localhost:1','data_url':'http://localhost:1','product':'kuasar'},tmp_path,scenario=Scenario())
    token=session.journal.reserve_sync(0)
    await session._N10()
    refs=json.loads((tmp_path/'evidence/raw_refs.json').read_text())
    ref=next(row for row in refs if row['logical_name']=='execution_journal.json')
    raw=json.loads((tmp_path/'evidence'/ref['path']).read_text())
    assert raw['operations'][token]['status']=='reserved'
    session.journal.own('sandbox','late',{'environment':session.environment})
    with pytest.raises(ValueError,match='seal.*changed|changed.*seal'):
        await session._N10()

@pytest.mark.asyncio
async def test_post_validation_uses_original_bytes_and_repeat_identity(tmp_path):
    import hashlib
    from sandbox_bench.session import BenchSession
    from sandbox_bench.runner import atomic_json
    data=sample();data['repeat_id']=3
    data['completion'].update(validation_policy='batch_after',command='echo',expected_stdout='\ufffd')
    session=BenchSession(CaseContract.model_validate(data),
        {'api_url':'http://localhost:1','data_url':'http://localhost:1','product':'kuasar'},tmp_path,scenario=Scenario())
    class BadUtf8(ValidationAdapter):
        def execute(self,*args):
            return {'stdout':'\ufffd','stdout_bytes':1,'stdout_sha256':hashlib.sha256(b'\xff').hexdigest(),'exit_code':0}
    session.adapter=BadUtf8()
    atomic_json(tmp_path/'load.json',{'started_at':0,'finished_at':2,'clock_domain':'c','events':[],
        'results':{'0':{'status':'success','value':{'sandbox_id':'s','completion':{'latency':1,'observed_at':1}}}}})
    assert (await session._validate_commands())[0]['status']=='invalid'
    assert session.records()['operations'][0]['repeat']==3

@pytest.mark.asyncio
async def test_host_and_provider_evidence_bytes_are_sealed(tmp_path):
    import json
    from sandbox_bench.session import BenchSession
    session=BenchSession(CaseContract.model_validate(sample()),
        {'api_url':'http://localhost:1','data_url':'http://localhost:1','product':'kuasar'},tmp_path,scenario=Scenario())
    for name in ('host_samples','provider_evidence'):
        (tmp_path/name).mkdir();(tmp_path/name/'sample.json').write_bytes(b'{"evidence":"exact"}\n')
    await session._N10()
    refs=json.loads((tmp_path/'evidence/raw_refs.json').read_text())
    for name in ('host_samples/sample.json','provider_evidence/sample.json'):
        ref=next(row for row in refs if row['logical_name']==name)
        assert (tmp_path/'evidence'/ref['path']).read_bytes()==b'{"evidence":"exact"}\n'

@pytest.mark.asyncio
async def test_baseline_deadline_keeps_pending_and_serializes_close(tmp_path):
    import threading
    import asyncio
    from sandbox_bench.session import BenchSession
    entered=threading.Event();release=threading.Event();closed=threading.Event()
    class Sampler:
        clock_domain='c'
        def sample_once(self):
            entered.set();release.wait(2)
            return {'sample_id':'one','seq':1,'kind':'sample_gap','through_seq':1,'dropped_count':1}
        def reconcile(self,sink):return {'status':'reconciled','pending':0}
        def close(self):closed.set()
    data=sample();data['resources']={'inventory_ref':'inventory','interval_s':.01,'window_s':.01,'min_samples':2,'max_gap_s':.02,'max_wait_s':.03}
    data['cleanup_timeout_s']=.03
    session=BenchSession(CaseContract.model_validate(data),{'api_url':'http://localhost:1','data_url':'http://localhost:1','product':'kuasar'},tmp_path,scenario=Scenario(),sampler=Sampler(),clock_domain='c')
    try:
        result=await asyncio.wait_for(session._N04(),.2)
        assert result['status']=='unknown'
        assert session.journal.state()['observation_pending'] and entered.is_set()
        assert (await session._N07())['status']=='unknown'
        assert not closed.is_set()
    finally:release.set()
    await asyncio.sleep(.05)
    assert closed.is_set()
    assert (await session._N07())['status']=='completed'
    assert not session.journal.state()['observation_pending']

@pytest.mark.asyncio
async def test_cancelled_parent_keeps_durable_success_child_and_rejects_conflict(tmp_path):
    from sandbox_bench.session import BenchSession
    from sandbox_bench.runner import atomic_json
    from sandbox_bench.analysis import analyze_case
    data=sample();data.update(method_id='B05.NATIVE',clone_count=2);data['load']['requests']=4
    session=BenchSession(CaseContract.model_validate(data),{'api_url':'http://localhost:1','data_url':'http://localhost:1','product':'kuasar'},tmp_path,scenario=Scenario(),clock_domain='c')
    token=session.journal.reserve_sync(0,2);session.journal.submission_started_sync(token)
    child={'status':'success','sandbox_id':'c1','completion':{'latency':1,'observed_at':1}}
    await session.journal.record_child_terminal(token,0,OperationResult('success',value=child),1)
    session.journal.terminal_sync(token,{'status':'unknown','value':None})
    report={'started_at':0,'finished_at':2,'clock_domain':'c','events':[],
            'results':{'0':{'status':'unknown','value':None}}}
    atomic_json(tmp_path/'load.json',report)
    records=session.records()
    assert [r['status'] for r in records['operations']]==['success','unknown']
    assert list(session._subjects(report))[0][1]['sandbox_id']=='c1'
    assert analyze_case(records)['throughput']['count']==1
    report['results']['0']['value']={'children':[dict(child,status='failed')]}
    atomic_json(tmp_path/'load.json',report)
    with pytest.raises(ValueError,match='child'):session.records()
