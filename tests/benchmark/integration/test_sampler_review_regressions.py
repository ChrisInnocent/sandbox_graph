"""Round1: actual OperationService receipts, stop authority and native owner races."""
import asyncio
from copy import deepcopy
import threading

import pytest

from sandbox_bench.bridge import BenchGateway
from sandbox_graph.domain.models import Operation,RunRecord,ScriptRef,canonical_hash
from sandbox_graph.runtime.leases import LeaseService
from sandbox_graph.runtime.observation import ObservationService
from sandbox_graph.runtime.runner import OperationService
from sandbox_graph.services.lifecycle_safety import LifecycleSafetyService
from sandbox_graph.services.revisions import RevisionService
from tests.benchmark.unit.test_sampler_lifecycle import ControlledSampler,session
from tests.orchestration.conftest import MemoryRepository,FakeClock


def setup(tmp_path,sampler):
    bench=session(tmp_path,sampler,baseline_wait=.03,cleanup_wait=.03)
    repo=MemoryRepository(FakeClock())
    RevisionService(repo).create(RunRecord(run_id='r',root_thread_id='t',graph_ref='graph1',
        contract_ref='contracthash',active_plan_ref='plan1',auth_ref='a',lineage_id='r'))
    token=LeaseService(repo).acquire_thread('t','worker',100)
    with repo.transaction() as tx:
        tx.put('work_instances','w',dict(work_id='w',run_id='r',group_id='g',plan_ref='plan1',
            graph_ref='graph1',contract_ref='contracthash',key={'measurement_generation':3}))
    LifecycleSafetyService(repo).acquire_work('w',token)
    script=ScriptRef(script_id='benchmark.step',version='1',digest='a'*64)
    gateway=BenchGateway({'w':bench},script)
    return repo,token,bench,gateway,OperationService(repo,gateway,{('benchmark.step','1'):script})


def operation(bench,gateway,node):
    payload={'step_id':node,'case_hash':bench.case.digest()}
    return Operation(operation_id='original-'+node,input_hash=canonical_hash(payload),input=payload,
        script_ref=gateway.script_ref,scope='w',deadline=1050,node_id=node,run_id='r',
        submission_kind='cleanup' if node=='N07' else 'ordinary')


@pytest.mark.asyncio
async def test_failed_n04_outcome_survives_stop_and_query_with_insufficient_data(tmp_path):
    sampler=ControlledSampler(pending=True)
    repo,token,bench,gateway,service=setup(tmp_path,sampler)
    op=operation(bench,gateway,'N04')
    initial=await service.submit(op,token)
    assert initial.state=='failed'
    original=deepcopy(bench.journal.receipt('N04:'+op.operation_id,op.input_hash))
    sampler.pending=False
    stopped=await service.cancel(op.operation_id,'w','stop original',token)
    assert stopped.state=='failed', 'cleanup completed cannot replace the failed baseline outcome'
    assert stopped.output['data_status']=='insufficient'
    assert stopped.output['sampler_stop']['status']=='completed'
    queried=await service.reconcile(op.operation_id,'w',token)
    assert queried.state=='failed' and queried.output['data_status']=='insufficient'
    assert bench.journal.receipt('N04:'+op.operation_id,op.input_hash)==original
    with repo.transaction() as tx:
        assert tx.get('operations',op.operation_id)['body']['status']['state']=='failed'
        assert ObservationService.closed_in(tx,tx.scan('observations')[0])


@pytest.mark.asyncio
async def test_session_cancel_flag_does_not_authorize_query_to_submit_native_close(tmp_path):
    sampler=ControlledSampler()
    repo,token,bench,gateway,service=setup(tmp_path,sampler)
    op=operation(bench,gateway,'N04')
    await service.submit(op,token)
    await bench.cancel('admission cancellation only')
    await asyncio.sleep(.01)
    try:
        await service.reconcile(op.operation_id,'w',token)
        assert not sampler.closed, 'query must not perform the first native close'
        assert bench._sampler_close_future is None
        with repo.transaction() as tx:
            assert tx.scan('cancellations')==[]
            assert tx.scan('sampler_stop_intents')==[]
            assert not ObservationService.closed_in(tx,tx.scan('observations')[0])
    finally:
        await bench._N07()


@pytest.mark.asyncio
async def test_n07_only_old_native_close_cannot_be_closed_by_replacement_session(tmp_path):
    class SlowClose(ControlledSampler):
        def close(self):
            self.entered.set()
            self.release.wait(3)
            self.closed=True
    sampler=SlowClose()
    repo,token,bench,gateway,service=setup(tmp_path,sampler)
    await service.submit(operation(bench,gateway,'N00'),token)
    op=operation(bench,gateway,'N07')
    assert (await service.submit(op,token)).state=='unknown'
    assert sampler.entered.is_set() and not bench._sampler_close_future.done()
    replacement_sampler=ControlledSampler()
    replacement=session(tmp_path,replacement_sampler,baseline_wait=.03,cleanup_wait=.03)
    replacement_gateway=BenchGateway({'w':replacement},gateway.script_ref)
    takeover=OperationService(repo,replacement_gateway,service.scripts)
    try:
        result=await takeover.cancel(op.operation_id,'w','stop original',token)
        assert result.state=='unknown'
        await takeover.collect(op.operation_id,'w',token)
        with repo.transaction() as tx:
            assert not any(ObservationService.closed_in(tx,row) for row in tx.scan('observations'))
        assert not replacement_sampler.closed
        assert not bench._sampler_close_future.done()
    finally:
        sampler.release.set()
        await asyncio.sleep(.01)
        await bench._N07()
        replacement._sampler_pool.shutdown(wait=True)


@pytest.mark.asyncio
async def test_baseline_timeout_stops_as_cancelled_and_driver_can_close_without_success(tmp_path):
    from langgraph.checkpoint.memory import InMemorySaver
    from sandbox_bench.bridge import work_handlers
    from sandbox_graph.graphs.factory import build_work
    from sandbox_graph.runtime.driver import Activation,Driver
    sampler=ControlledSampler(block_at=1)
    repo,token,bench,gateway,service=setup(tmp_path,sampler)
    op=operation(bench,gateway,'N04')
    assert (await service.submit(op,token)).state=='unknown'
    original=deepcopy(bench.journal.receipt('N04:'+op.operation_id,op.input_hash))
    try:
        result=await service.cancel(op.operation_id,'w','stop original',token)
        assert result.state=='unknown'
        issued_close=bench._sampler_close_future
    finally:
        sampler.release.set()
    for _ in range(100):
        if bench._sampler_shutdown_future.done():break
        await asyncio.sleep(.002)
    result=await service.reconcile(op.operation_id,'w',token)
    assert result.state=='cancelled' and result.authoritative
    assert result.output['data_status']=='insufficient'
    assert result.output['original_result']==original
    assert bench._sampler_close_future is issued_close
    raw={p:p.read_bytes() for p in (bench.folder/'resource_samples').glob('*.json')}
    LifecycleSafetyService(repo).release_work('w',token)
    with repo.transaction() as tx:tx.release_thread(token)
    driver=Driver(repo,{},lambda _:InMemorySaver(),build_work,handler_factory=lambda current,_:
        work_handlers(service,current,bench,run_id='r',scope='w',dispatch_generation=1,deadline=1050))
    output=await driver.start(Activation(activation_id='timeout-drain',run_id='r',thread_id='t',scope='w',
        plan_ref='plan1',graph_ref='graph1',input_refs={'case':bench.case.digest()},lease_seconds=100,segment_seconds=20))
    assert all(n in output['result_refs'] for n in ('N07','N09','N10'))
    with repo.transaction() as tx:
        owner=tx.acquire_thread('closed-check','review',30)
        LifecycleSafetyService.acquire_work_in(tx,'w',owner,purpose='reconcile')
        ref=LifecycleSafetyService.produce_disposition_in(tx,'w',owner,ttl=10)
        disposition=LifecycleSafetyService.verify_disposition_in(tx,ref,'w',owner)
        assert disposition.stopped and disposition.sampler_status=='stopped'
        assert tx.get('operations',op.operation_id)['body']['status']['state']=='cancelled'
    assert bench.journal.receipt('N04:'+op.operation_id,op.input_hash)==original
    assert all(p.read_bytes()==value for p,value in raw.items())
    assert sampler.calls==1


@pytest.mark.asyncio
async def test_existing_stop_query_rechecks_deadline_without_new_native_work(tmp_path):
    from sandbox_graph.persistence.repository import Conflict
    sampler=ControlledSampler(block_at=1)
    repo,token,bench,gateway,service=setup(tmp_path,sampler)
    op=operation(bench,gateway,'N04')
    await service.submit(op,token)
    await service.cancel(op.operation_id,'w','stop original',token)
    close=bench._sampler_close_future
    repo.clock.advance(51)
    try:
        with pytest.raises(Conflict,match='deadline'):
            await service.reconcile(op.operation_id,'w',token)
        assert bench._sampler_close_future is close
    finally:
        sampler.release.set()
        await asyncio.sleep(.01)
        await bench._N07()


@pytest.mark.asyncio
@pytest.mark.parametrize('node',['N01','N05'])
async def test_non_sampler_cancel_and_query_do_not_enter_sampler_intent_protocol(tmp_path,node):
    sampler=ControlledSampler()
    repo,token,bench,gateway,service=setup(tmp_path,sampler)
    await service.submit(operation(bench,gateway,'N00'),token)
    op=operation(bench,gateway,node)
    original=await service.submit(op,token)
    receipt=deepcopy(bench.journal.receipt(node+':'+op.operation_id,op.input_hash))
    await service.cancel(op.operation_id,'w','cancel original non-sampler',token)
    with repo.transaction() as tx:
        assert tx.get('sampler_stop_intents',op.operation_id) is None
    await service.submit(operation(bench,gateway,'N07'),token)
    for _ in range(2):
        current=await service.reconcile(op.operation_id,'w',token)
        assert current==original
    assert bench.journal.receipt(node+':'+op.operation_id,op.input_hash)==receipt
