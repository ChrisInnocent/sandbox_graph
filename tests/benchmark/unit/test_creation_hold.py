"""RT-49: held resource window is separate from creation timing and actual cohort."""
import pytest
from sandbox_bench.creation_hold import hold_created, HOLD_METHODS
from sandbox_bench.contracts import CaseContract
from tests.benchmark.unit.test_contract_models import sample

RESOURCE={'interval_s':1.0,'window_s':2.0,'max_wait_s':4.0,'max_gap_s':1.1,
    'min_samples':3,'inventory_ref':'owned-processes'}


def held_case(**changes):
    raw=sample();raw.update(method_id='B01.TEMPLATE.HOLD',graph_ref='hold:1',resources=RESOURCE)
    raw.update(changes)
    return CaseContract.model_validate(raw)


@pytest.mark.parametrize('change',[{'resources':None},{'resource_observation_waived':True},{'graph_ref':'graph1'},
    {'ttl_s':30.0}])
def test_hold_requires_observation_budget_and_new_graph_version(change):
    with pytest.raises(ValueError): held_case(**change)


def test_every_hold_method_is_registered_in_both_catalogues():
    from sandbox_bench.registry import METHODS
    from sandbox_graph.catalog.registry import METHODS as GRAPH
    assert set(HOLD_METHODS)=={'B01.TEMPLATE.HOLD','B01.SNAPSHOT.DISK.HOLD','B01.SNAPSHOT.MEMORY.HOLD',
        'B02.FULL.HOLD','B02.PREPARED.HOLD'}
    assert set(HOLD_METHODS)<=set(METHODS)&set(GRAPH)


class Clock:
    now=0.0
    def __call__(self): return self.now
    async def sleep(self,n): self.now+=n


def snapshots():
    return [{'kind':'resource_sample','t_s':float(i),'clock_domain':'d',
        'inventory_complete':True,'snapshot_cohort_ids':['actually-ready'],
        'sample_id':str(i),'processes':[{'sandbox_id':'actually-ready','pss_bytes':100,
            'cpu_seconds':float(i),'roles':['compute'],'components':['sandbox']}]} for i in range(3)]


@pytest.mark.asyncio
async def test_hold_waits_for_window_and_uses_observed_not_requested_denominator():
    clock=Clock();calls=[]
    async def inspect(handle,timeout):
        calls.append((handle['sandbox_id'],clock()))
        return {'state':'running'}
    async def steady(): return {'status':'completed','steady':{'status':'steady'}}
    result=await hold_created(held_case(),['actually-ready'],inspect=inspect,steady=steady,
        samples=snapshots,clock_domain='d',clock=clock,sleep=clock.sleep)
    assert result['status']=='completed'
    assert result['requested_count']==5 and result['actual_ready_count']==1
    assert result['window']=={'start_s':0.0,'end_s':2.0,'clock_domain':'d','window_kind':'creation_hold'}
    assert calls==[('actually-ready',0.0),('actually-ready',2.0)]
    assert result['sample_refs']==['0','1','2']


@pytest.mark.asyncio
@pytest.mark.parametrize('fault',['missing_sample','changed_cohort','wrong_clock','missing_pss','lost_ready'])
async def test_hold_does_not_claim_covered_resources_when_evidence_is_missing(fault):
    clock=Clock();rows=snapshots()
    if fault=='missing_sample': rows.pop(1)
    if fault=='changed_cohort': rows[1]['snapshot_cohort_ids']=[]
    if fault=='wrong_clock': rows[1]['clock_domain']='other'
    if fault=='missing_pss': rows[1]['processes'][0]['pss_bytes']=None
    async def inspect(handle,timeout):
        return {'state':'paused' if fault=='lost_ready' and clock()>0 else 'running'}
    async def steady(): return {'status':'completed','steady':{'status':'steady'}}
    result=await hold_created(held_case(),['actually-ready'],inspect=inspect,steady=steady,
        samples=lambda:rows,clock_domain='d',clock=clock,sleep=clock.sleep)
    assert result['status']=='blocked'
    assert result['resource_status']=='unavailable'


@pytest.mark.asyncio
async def test_real_session_creates_holds_stops_sampler_then_deletes_and_seals(tmp_path):
    import json
    import time
    from sandbox_bench.session import BenchSession
    from sandbox_bench.scenarios import ScenarioExecutor
    from sandbox_bench.analysis import recompute_case
    from scripts.mock_pipeline import ScriptedResourceSampler, scripted_steady

    case=held_case(resources={**RESOURCE,'interval_s':.01,'window_s':.04,
        'max_wait_s':1.,'max_gap_s':.3},load={'mode':'WINDOW','requests':1,
        'concurrency':1,'run_timeout_s':1.,'max_unknown_external':1})
    events=[]
    class Adapter:
        product='kuasar'
        deleted=False
        config={'source':{'kind':'template','id':'e2b-snp-x'}}
        def create(self,owner,timeout):
            events.append(('create',time.monotonic()))
            return {'sandbox_id':'one'}
        def inspect(self,handle,timeout):
            events.append(('inspect',time.monotonic()))
            return {'state':'absent' if self.deleted else 'running'}
        def delete(self,handle,timeout):
            events.append(('delete',time.monotonic()))
            self.deleted=True
        def execute(self,*args):
            raise AssertionError('STATE_READY must not execute a command')
    process={'identity':{'environment':'envhash','node':'n','boot_id':'boot','pid':42,'start_ticks':1},
        'roles':['compute'],'components':['sandbox'],'sandbox_id':'one','generation':'g1',
        'pss_bytes':2048,'cpu_seconds':0.}
    sampler=ScriptedResourceSampler([process],'hold-test',case.work_id)
    bench=BenchSession(case,{'product':'kuasar','api_url':'http://localhost:1',
        'data_url':'http://localhost:1'},tmp_path,sampler=sampler,
        steady_check=scripted_steady,clock_domain='hold-test')
    bench.adapter=Adapter()
    bench.scenario=ScenarioExecutor(case,bench.adapter,on_owned=bench._owned)
    try:
        for step in ('N04','N05','N06','N07'):
            assert (await bench.apply_step(step,step))['status']=='completed'
        hold=json.loads((tmp_path/'hold.json').read_text())
        load=json.loads((tmp_path/'load.json').read_text())
        assert load['finished_at']<=hold['started_s']<=hold['window']['start_s']
        assert not any(event[0]=='delete' for event in events)
        assert bench.sampler_stop_status()['sampler_receipt']['state']=='stopped'
        assert (await bench.apply_step('N09','clean'))['status']=='completed'
        assert next(at for kind,at in events if kind=='delete')>=hold['finished_s']
        sealed=await bench.apply_step('N10','seal')
        if sealed['status']!='completed': await bench._N10()
        assert sealed['status']=='completed',sealed
        summary=recompute_case(tmp_path/'evidence')
        resource=summary['resources']['creation_hold']
        assert resource['actual_ready_count']==1
        assert resource['final_cohort']['direct_mean_bytes']==2048
        assert resource['memory_basis']=='raw_pss_including_guest'
        assert summary['throughput']['count']==1
        refs=json.loads((tmp_path/'evidence'/'raw_refs.json').read_text())
        assert any(row['logical_name']=='hold.json' for row in refs)
    finally:
        await bench._N07()
