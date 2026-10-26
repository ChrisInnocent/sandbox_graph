import asyncio
import pytest
from sandbox_bench.sched_observation import SchedObservation, analyze_trace


class Collector:
    clock_domain='ktime:test'
    lost_events=0
    dropped_events=0
    def __init__(self): self.closed=False
    def start(self): return {'available':True}
    def poll(self,timeout_ms=10): pass
    def stop(self):
        self.closed=True
        return [{'t_ns':10,'prev_pid':0,'next_pid':12},
            {'t_ns':30,'prev_pid':12,'next_pid':0}]


INVENTORY=[{'identity':{'environment':'e','node':'n','boot_id':'b','pid':12,'start_ticks':1},
    'roles':['compute'],'components':['vmm']}]


@pytest.mark.asyncio
async def test_observation_lifecycle_uses_trace_boundaries_and_detaches():
    clock=iter([0,15,25,40])
    collector=Collector()
    obs=SchedObservation(collector,inventory=lambda:INVENTORY,clock_ns=lambda:next(clock),
        pre_roll_s=.001,post_roll_s=.001,verify_inventory=lambda rows:rows)
    assert (await obs.start())['status']=='running'
    obs.begin('create');obs.end('create')
    result=await obs.stop(timeout_s=1)
    assert result['status']=='completed' and collector.closed
    assert result['windows']['create']=={'start_ns':15,'end_ns':25,'clock_domain':'ktime:test'}
    summary=analyze_trace(result)
    assert summary['status']=='valid' and summary['true_peak_cores']==1.0
    assert summary['by_role']=={'compute':1.0}
    assert summary['sampled_peak_cores'] is None
    assert await obs.stop(timeout_s=1)==result


@pytest.mark.parametrize('mutation,reason',[
    ({'lost_events':1},'trace_events_lost'),
    ({'dropped_events':1},'trace_events_lost'),
    ({'inventory_changed':True},'inventory_changed'),
    ({'clock_domain':'other'},'trace_window_clock_mismatch'),
    ({'status':'unknown'},'trace_stop_unconfirmed')])
def test_missing_coverage_never_claims_a_valid_exact_peak(mutation,reason):
    record={'status':'completed','events':[{'t_ns':10,'prev_pid':0,'next_pid':12},
        {'t_ns':30,'prev_pid':12,'next_pid':0}],'inventory':INVENTORY,
        'clock_domain':'ktime:test','windows':{'create':{'start_ns':15,'end_ns':25,'clock_domain':'ktime:test'}},
        'lost_events':0,'dropped_events':0,'inventory_changed':False}
    result=analyze_trace(record|mutation)
    assert result['status']!='valid' and result['reason']==reason
    if result['status']=='unavailable': assert result['true_peak_cores'] is None


@pytest.mark.asyncio
async def test_unavailable_backend_preserves_explicit_fallback_and_no_polling():
    class Unavailable(Collector):
        def start(self): raise RuntimeError('bcc unavailable')
        def poll(self,**kwargs): raise AssertionError('must not poll')
    obs=SchedObservation(Unavailable(),inventory=lambda:INVENTORY)
    assert (await obs.start())['status']=='unavailable'
    obs.begin('create');obs.end('create')
    result=await obs.stop(timeout_s=.1)
    assert analyze_trace(result)['true_peak_cores'] is None


def test_bpf_collector_records_ring_overflow_and_detaches():
    from sandbox_bench.sched_peak import BpfSchedSwitchCollector
    from types import SimpleNamespace
    class BPF:
        cleaned=False
        def __getitem__(self,key): return self
        def event(self,data): return SimpleNamespace(t_ns=1,prev_pid=12,next_pid=0)
        def cleanup(self): self.cleaned=True
    collector=BpfSchedSwitchCollector(run_id='r',clock_domain='d',ring_capacity=1)
    bpf=BPF();collector._bpf=bpf
    collector._on_event(0,None,0);collector._on_event(0,None,0)
    collector._on_lost(0,3)
    assert collector.dropped_events==1 and collector.lost_events==3
    assert len(collector.stop())==1 and bpf.cleaned and collector._bpf is None


@pytest.mark.asyncio
async def test_session_wires_trace_start_window_stop_and_raw_seal(tmp_path):
    import json
    from tests.benchmark.unit.test_sampler_lifecycle import session,ControlledSampler
    collector=Collector()
    obs=SchedObservation(collector,inventory=lambda:INVENTORY,
        pre_roll_s=.001,post_roll_s=.001,verify_inventory=lambda rows:rows)
    class GapSampler(ControlledSampler):
        def sample_once(self):
            row=super().sample_once();row['dropped_count']=1
            return row
    bench=session(tmp_path,GapSampler(),baseline_wait=.5,cleanup_wait=1.)
    bench.sched_observer=obs
    try:
        for step in ('N04','N05','N07','N09','N10'):
            result=await bench.apply_step(step,step)
            assert result['status']=='completed',(step,result)
        record=json.loads((tmp_path/'sched.json').read_text())
        span=record['windows']['create']
        assert record['started_ns']<=span['start_ns']<span['end_ns']<=record['finished_ns']
        assert span['clock_domain']=='ktime:test' and span['clock_domain']!=bench.clock_domain
        refs=json.loads((tmp_path/'evidence'/'raw_refs.json').read_text())
        assert any(row['logical_name']=='sched.json' for row in refs)
        assert collector.closed
    finally: await bench._N07()
