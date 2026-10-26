"""真实 BenchSession 线程池退出边界；受控 sampler 只读且不访问沙箱。"""
import asyncio
import threading

import pytest

from sandbox_bench.contracts import CaseContract
from sandbox_bench.session import BenchSession
from tests.benchmark.unit.test_contract_models import sample
from tests.benchmark.unit.test_session import Scenario


class ControlledSampler:
    clock_domain = 'host-clock'
    safe_to_submit = True

    def __init__(self, *, block_at=None, pending=False):
        self.block_at, self.pending = block_at, pending
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls = 0
        self.closed = False
        self.samples = []

    def sample_once(self):
        self.calls += 1
        if self.calls == self.block_at:
            self.entered.set()
            self.release.wait(3)
        row = {'seq': self.calls, 'sample_id': f'sample:{self.calls}', 'kind': 'sample_gap',
               'through_seq': self.calls, 'reason': 'controlled_read_only_fixture'}
        self.samples.append(row)
        return row

    def reconcile(self, sink):
        if self.pending:
            return {'status': 'disconnected', 'pending': len(self.samples)}
        for row in self.samples:
            sink(row)
        return {'status': 'reconciled', 'pending': 0}

    def close(self):
        self.closed = True


def session(tmp_path, sampler=None, *, resources=True, baseline_wait=.02, cleanup_wait=.03):
    body = sample()
    body['cleanup_timeout_s'] = cleanup_wait
    if resources:
        body['resources'] = dict(interval_s=.01, window_s=.1, max_wait_s=baseline_wait,
            max_gap_s=.1, min_samples=1, inventory_ref='read-only-fixture')
    return BenchSession(CaseContract.model_validate(body),
        {'product':'kuasar','api_url':'http://localhost:1','data_url':'http://localhost:1'},
        tmp_path, scenario=Scenario(), sampler=sampler, clock_domain='host-clock')


@pytest.mark.asyncio
async def test_receipt_proves_own_task_capture_close_and_reentry_without_new_samples(tmp_path):
    sampler = ControlledSampler()
    bench = session(tmp_path, sampler)
    await bench.apply_step('N04', 'baseline')
    await asyncio.sleep(.01)
    result = await bench._N07()
    proof = result.get('sampler_receipt')
    assert proof is not None, 'N07 must expose the real in-process stop receipt'
    assert proof['state'] == 'stopped'
    assert proof['runtime_kind'] == 'inprocess_threadpool'
    assert proof['capture_submitted'] == proof['capture_completed'] >= 1
    assert proof['loop_exit'] == 'completed'
    assert proof['close_exit'] == 'completed'
    assert proof['process_identity']['pid'] > 0
    assert proof['capture_worker_id'] == proof['close_worker_id'] > 0
    calls = sampler.calls
    assert (await bench._N07())['sampler_receipt'] == proof
    assert (await bench.apply_step('N04', 'baseline'))['status'] == 'unknown'
    assert sampler.calls == calls


@pytest.mark.asyncio
@pytest.mark.parametrize('block_at', [1, 2])
async def test_live_capture_never_gets_stop_receipt_even_after_baseline_timeout(tmp_path, block_at):
    sampler = ControlledSampler(block_at=block_at)
    bench = session(tmp_path, sampler)
    result = await bench.apply_step('N04', 'baseline')
    if block_at == 1:
        assert result['reason'] == 'baseline_sample_deadline'
    else:
        for _ in range(100):
            if sampler.entered.is_set():
                break
            await asyncio.sleep(.001)
    assert sampler.entered.is_set()
    stop = await bench._N07()
    assert stop['status'] == 'unknown'
    assert 'sampler_receipt' not in stop
    assert not sampler.closed
    sampler.release.set()
    await asyncio.sleep(.01)
    done = await bench._N07()
    assert done['sampler_receipt']['state'] == 'stopped'
    assert done['sampler_receipt']['capture_completed'] == sampler.calls


@pytest.mark.asyncio
async def test_tail_not_acknowledged_and_new_process_epoch_cannot_prove_original_stop(tmp_path):
    sampler = ControlledSampler(pending=True)
    bench = session(tmp_path, sampler)
    await bench.apply_step('N04', 'baseline')
    result = await bench._N07()
    assert result['status'] == 'unknown' and 'sampler_receipt' not in result
    restored_sampler = ControlledSampler()
    restored = session(tmp_path, restored_sampler)
    stop = await restored._N07()
    assert stop['status'] == 'unknown', 'a new session epoch cannot close the original task'
    assert restored_sampler.calls == 0
    sampler.pending = False
    assert (await bench._N07())['sampler_receipt']['state'] == 'stopped'
    restored._sampler_pool.shutdown(wait=False)


@pytest.mark.asyncio
async def test_no_resource_contract_has_real_not_started_receipt(tmp_path):
    bench = session(tmp_path, resources=False)
    result = await bench._N07()
    assert result['sampler_receipt']['state'] == 'not_started'


@pytest.mark.asyncio
async def test_changed_boot_or_process_identity_cannot_publish_stop(tmp_path, monkeypatch):
    import sandbox_bench.session as module
    sampler=ControlledSampler()
    bench=session(tmp_path,sampler)
    await bench.apply_step('N04','baseline')
    original=module.process_identity
    monkeypatch.setattr(module,'process_identity',lambda:dict(original(),boot_id='another-boot'))
    result=await bench._N07()
    assert result['status']=='unknown' and 'sampler_receipt' not in result
    monkeypatch.setattr(module,'process_identity',original)
    assert (await bench._N07())['sampler_receipt']['state']=='stopped'


@pytest.mark.asyncio
async def test_real_resource_sampler_spool_closes_after_proc_fixture_capture(tmp_path):
    from sandbox_bench.sampler import ResourceSampler
    from tests.benchmark.unit.test_sampler import SamplerTests
    proc,inventory=SamplerTests().fixture(tmp_path)
    sampler=ResourceSampler(inventory,tmp_path/'spool.db',proc_root=proc,clock_ticks=100,
        clock_domain='host-clock',run_id='w')
    bench=session(tmp_path/'bench',sampler,baseline_wait=1.,cleanup_wait=1.)
    assert (await bench.apply_step('N04','baseline'))['status']=='completed'
    stop=await bench._N07()
    assert stop['sampler_receipt']['state']=='stopped'
    assert sampler._closed
    assert bench.records()['resource_series'][0]['processes'][0]['pss_bytes']==20480


@pytest.mark.asyncio
async def test_baseline_timeout_still_has_a_versioned_running_artifact(tmp_path):
    sampler=ControlledSampler(block_at=1)
    bench=session(tmp_path,sampler)
    try:
        assert (await bench.apply_step('N04','baseline'))['status']=='unknown'
        assert any(p['state']=='running' for p in bench._lifecycle_artifacts.values())
    finally:
        sampler.release.set()
        await bench._N07()


@pytest.mark.asyncio
async def test_capture_tracking_keeps_only_inflight_future_while_retaining_total_counts(tmp_path):
    sampler=ControlledSampler()
    bench=session(tmp_path,sampler)
    await bench.apply_step('N04','baseline')
    for _ in range(100):
        if sampler.calls>=5: break
        await asyncio.sleep(.005)
    assert sampler.calls>=5
    assert len(bench._capture_futures)<=1
    stop=await bench._N07()
    assert stop['sampler_receipt']['capture_submitted']==sampler.calls
    assert stop['sampler_receipt']['capture_completed']==sampler.calls


@pytest.mark.asyncio
async def test_cancelled_async_task_does_not_hide_running_capture_and_executor_is_joined(tmp_path):
    sampler=ControlledSampler(block_at=2)
    bench=session(tmp_path,sampler)
    await bench.apply_step('N04','baseline')
    for _ in range(100):
        if sampler.entered.is_set(): break
        await asyncio.sleep(.001)
    bench._sample_task.cancel()
    await asyncio.gather(bench._sample_task,return_exceptions=True)
    assert bench._sample_task.done() and bench._sample_task.cancelled()
    try:
        stop=await bench._N07()
        assert stop['status']=='unknown' and 'sampler_receipt' not in stop
        assert not sampler.closed
    finally:
        sampler.release.set()
    await asyncio.sleep(.01)
    stop=await bench._N07()
    assert stop['sampler_receipt']['loop_exit']=='cancelled'
    assert stop['sampler_receipt']['executor_exit']=='joined'
    assert all(not thread.is_alive() for thread in bench._sampler_pool._threads)


@pytest.mark.asyncio
async def test_interrupted_baseline_stays_insufficient_after_native_capture_finishes(tmp_path):
    class NoGapSampler(ControlledSampler):
        def sample_once(self):
            row=super().sample_once()
            row['kind']='resource_sample'
            return row
    sampler=NoGapSampler(block_at=1)
    bench=session(tmp_path,sampler)
    assert (await bench.apply_step('N04','baseline'))['status']=='unknown'
    sampler.release.set()
    await asyncio.sleep(.01)
    stop=await bench._N07()
    assert stop['sampler_receipt']['state']=='stopped'
    assert stop['sampler_receipt']['has_gaps'] is True
