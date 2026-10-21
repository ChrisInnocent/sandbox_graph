import time
import pytest
from sandbox_bench.source_lifecycle import SourceLedger


def proof(exists=True, consumers=()):
    return {'environment_ref':'env','source_id':'s','complete':True,'exists':exists,
            'consumer_refs':list(consumers),'observed_wall_s':time.time(),'evidence_ref':'fixture-inspection'}


@pytest.mark.asyncio
async def test_shared_pins_and_borrowed_sources_never_get_deleted(tmp_path):
    ledger=SourceLedger(tmp_path/'sources.json','env')
    ledger.register('s',owner_work='w',creation_evidence_ref='creation')
    ledger.pin('s','child-a');ledger.pin('s','child-b')
    calls=[];exists=True
    async def inspect(sid):return proof(exists)
    async def delete(sid,timeout):
        nonlocal exists
        calls.append(sid);exists=False
    async def clean():return await ledger.cleanup('s',owner_work='w',inspector=inspect,deleter=delete,timeout_s=1,max_age_s=1)
    assert (await clean())['status']=='retained'
    ledger.release('s','child-a',evidence_ref='child-a-absent')
    assert (await clean())['status']=='retained'
    ledger.release('s','child-b',evidence_ref='child-b-absent')
    assert (await clean())['status']=='complete' and calls==['s']
    assert (await clean())['status']=='complete' and calls==['s']
    with pytest.raises(ValueError):ledger.pin('s','late')
    ledger.register('borrowed',owner_work='w',creation_evidence_ref='external',borrowed=True)
    assert (await ledger.cleanup('borrowed',owner_work='w',inspector=inspect,deleter=delete,
                                 timeout_s=1,max_age_s=1))['reason']=='borrowed_source'


@pytest.mark.asyncio
async def test_unknown_delete_survives_reopen_and_requires_absence_evidence(tmp_path):
    path=tmp_path/'sources.json';ledger=SourceLedger(path,'env')
    ledger.register('s',owner_work='w',creation_evidence_ref='creation')
    calls=[]
    async def inspect(sid):return proof()
    async def lost(sid,timeout):calls.append(sid);raise ConnectionError()
    assert (await ledger.cleanup('s',owner_work='w',inspector=inspect,deleter=lost,
                                 timeout_s=1,max_age_s=1))['status']=='unknown'
    restored=SourceLedger(path,'env')
    assert (await restored.cleanup('s',owner_work='w',inspector=inspect,deleter=lost,
                                   timeout_s=1,max_age_s=1))['status']=='unknown'
    assert calls==['s']
    with pytest.raises(PermissionError):
        restored.reconcile_absent('s',owner_work='other',proof=proof(False),max_age_s=1)
    with pytest.raises(ValueError):
        restored.reconcile_absent('s',owner_work='w',proof=proof(),max_age_s=1)
    assert restored.reconcile_absent('s',owner_work='w',proof=proof(False),max_age_s=1)['status']=='complete'


@pytest.mark.asyncio
async def test_external_consumer_or_stale_proof_blocks_cleanup(tmp_path):
    ledger=SourceLedger(tmp_path/'sources.json','env')
    ledger.register('s',owner_work='w',creation_evidence_ref='creation')
    async def inspect(sid):return proof(consumers=('other-work',))
    async def forbidden(*args):raise AssertionError('must not delete')
    result=await ledger.cleanup('s',owner_work='w',inspector=inspect,deleter=forbidden,timeout_s=1,max_age_s=1)
    assert result['reason']=='external_consumers_present'
    async def stale(sid):return {**proof(),'observed_wall_s':0}
    assert (await ledger.cleanup('s',owner_work='w',inspector=stale,deleter=forbidden,
                                 timeout_s=1,max_age_s=1))['status']=='unknown'


@pytest.mark.asyncio
async def test_noncooperative_callback_deadline_is_bounded_without_replay(tmp_path):
    import asyncio
    ledger=SourceLedger(tmp_path/'sources.json','env')
    ledger.register('s',owner_work='w',creation_evidence_ref='creation')
    release=asyncio.Event();entered=asyncio.Event();calls=[]
    async def inspect(sid):return proof()
    async def delete(sid,timeout):
        calls.append(sid);entered.set()
        try:await release.wait()
        except asyncio.CancelledError:await release.wait()
    task=asyncio.create_task(ledger.cleanup('s',owner_work='w',inspector=inspect,deleter=delete,timeout_s=.02,max_age_s=1))
    try:
        await entered.wait();await asyncio.sleep(.07)
        assert task.done(), 'callback cancellation must not extend cleanup deadline'
        assert task.result()['status']=='unknown'
        assert (await ledger.cleanup('s',owner_work='w',inspector=inspect,deleter=delete,timeout_s=.02,max_age_s=1))['status']=='unknown'
        assert calls==['s']
    finally:
        release.set();await task;await asyncio.sleep(.01)


@pytest.mark.asyncio
async def test_reconcile_cannot_overwrite_active_inspection(tmp_path):
    import asyncio
    ledger=SourceLedger(tmp_path/'sources.json','env')
    ledger.register('s',owner_work='w',creation_evidence_ref='creation')
    entered=asyncio.Event();release=asyncio.Event()
    async def inspect(sid):entered.set();await release.wait();return proof(False)
    async def forbidden(*args):raise AssertionError('no deletion')
    task=asyncio.create_task(ledger.cleanup('s',owner_work='w',inspector=inspect,deleter=forbidden,timeout_s=1,max_age_s=1))
    try:
        await entered.wait()
        with pytest.raises(ValueError,match='active'):
            ledger.reconcile_absent('s',owner_work='w',proof=proof(False),max_age_s=1)
    finally:release.set();await task


@pytest.mark.asyncio
async def test_old_cached_absence_is_not_post_delete_observation(tmp_path):
    ledger=SourceLedger(tmp_path/'sources.json','env')
    ledger.register('s',owner_work='w',creation_evidence_ref='creation')
    cached={**proof(False),'observed_wall_s':time.time()-.1};calls=[]
    async def inspect(sid):return proof() if not calls else cached
    async def delete(sid,timeout):calls.append(sid)
    result=await ledger.cleanup('s',owner_work='w',inspector=inspect,deleter=delete,timeout_s=1,max_age_s=1)
    assert result['status']=='unknown'
    assert calls==['s']


@pytest.mark.asyncio
async def test_reconcile_requires_finite_proof_age(tmp_path):
    ledger=SourceLedger(tmp_path/'sources.json','env')
    ledger.register('s',owner_work='w',creation_evidence_ref='creation')
    async def inspect(sid):raise ConnectionError()
    async def delete(*args):raise AssertionError('not called')
    await ledger.cleanup('s',owner_work='w',inspector=inspect,deleter=delete,timeout_s=1,max_age_s=1)
    with pytest.raises(ValueError):
        ledger.reconcile_absent('s',owner_work='w',proof={**proof(False),'observed_wall_s':0},max_age_s=float('inf'))


@pytest.mark.asyncio
async def test_pending_callback_cannot_be_reconciled_until_settled(tmp_path):
    import asyncio
    path=tmp_path/'sources.json';ledger=SourceLedger(path,'env')
    ledger.register('s',owner_work='w',creation_evidence_ref='creation')
    release=asyncio.Event()
    async def inspect(sid):return proof()
    async def delete(sid,timeout):await release.wait()
    result=await ledger.cleanup('s',owner_work='w',inspector=inspect,deleter=delete,timeout_s=.01,max_age_s=1)
    old_proof=proof(False)
    try:
        with pytest.raises(ValueError,match='active'):
            ledger.reconcile_absent('s',owner_work='w',proof=old_proof,max_age_s=1)
        restored=SourceLedger(path,'env')
        with pytest.raises(ValueError,match='settlement'):
            restored.reconcile_absent('s',owner_work='w',proof=old_proof,max_age_s=1)
        assert restored.snapshot()['sources']['s']['cleanup']['callback_pending']
    finally:release.set();await asyncio.sleep(.02)
    with pytest.raises(ValueError,match='predates'):
        restored.reconcile_absent('s',owner_work='w',proof=old_proof,max_age_s=1)
    assert restored.reconcile_absent('s',owner_work='w',proof=proof(False),max_age_s=1)['status']=='complete'
    assert len(result['operation_id'])==64


def test_interrupted_callback_needs_matching_settlement_proof(tmp_path):
    path=tmp_path/'sources.json';ledger=SourceLedger(path,'env')
    ledger.register('s',owner_work='w',creation_evidence_ref='creation')
    with ledger._transaction() as state:
        state['sources']['s']['cleanup']={'status':'unknown','operation_id':'op',
            'callback_pending':True,'started_wall_s':time.time()-.1}
    restored=SourceLedger(path,'env')
    with pytest.raises(ValueError,match='settlement'):
        restored.reconcile_absent('s',owner_work='w',proof={**proof(False),'operation_settled':True,'operation_id':'foreign'},max_age_s=1)
    assert restored.reconcile_absent('s',owner_work='w',proof={**proof(False),'operation_settled':True,'operation_id':'op'},max_age_s=1)['status']=='complete'
