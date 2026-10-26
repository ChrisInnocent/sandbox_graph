import importlib.util
import pytest
import json


def cls():
    assert importlib.util.find_spec('sandbox_bench.journal') is not None, 'durable journal missing'
    from sandbox_bench.journal import FileJournal
    return FileJournal


def test_budget_survives_reopen_and_unknown_does_not_resubmit(tmp_path):
    Journal=cls();j=Journal(tmp_path,'work',child_budget=5,parent_budget=3)
    token=j.reserve_sync(0,2)
    j.submission_started_sync(token)
    j=Journal(tmp_path,'work',child_budget=5,parent_budget=3)
    assert j.reserve_sync(0,2)==token
    with pytest.raises(ValueError):j.submission_started_sync(token)
    assert j.state()['child_reserved']==2
    with pytest.raises(ValueError):j.reserve_sync(1,4)
    assert j.state()['child_reserved']==2


def test_identity_conflict_receipts_and_scope_cleanup(tmp_path):
    Journal=cls();j=Journal(tmp_path,'work',child_budget=4,parent_budget=4)
    token=j.reserve_sync(0,1)
    with pytest.raises(ValueError):j.reserve_sync(0,2)
    j.own('sandbox','one',{'environment':'e','node':'n'})
    assert j.owned('e')[0]['resource_id']=='one'
    with pytest.raises(ValueError):j.owned('other')
    j.receipt('N02','hash',{'ref':'x'})
    assert j.receipt('N02','hash')=={'ref':'x'}
    with pytest.raises(ValueError):j.receipt('N02','changed')
    j.submission_started_sync(token)
    j.terminal_sync(token,{'status':'unknown'})
    assert j.operation(token)['status']=='unknown'
    events=j.collect(0)
    assert len({x['event_id'] for x in events})==len(events)
    assert j.collect(events[-1]['seq'])==[]


@pytest.mark.parametrize('child', [False, True])
def test_large_terminal_evidence_is_durable_without_rewriting_raw_in_ledger(tmp_path, child):
    import asyncio
    Journal=cls();j=Journal(tmp_path,'work',child_budget=2,parent_budget=2)
    token=j.reserve_sync(0);j.submission_started_sync(token)
    result={'status':'success','value':{'raw_log':'raw-evidence\n'*100000}}
    def record():
        if child:asyncio.run(j.record_child_terminal(token,0,result,1.0))
        else:j.terminal_sync(token,result)
    record()
    assert j.path.stat().st_size < 65536
    stored=json.loads(j.path.read_text())['operations'][token]
    stored=stored['children']['0'] if child else stored
    ref=stored['result_ref']
    blob=tmp_path/'results'/f"{ref['sha256']}.json"
    original=blob.read_bytes()
    assert json.loads(original)==result
    # Ordinary budget writes and idempotent terminal replay keep immutable evidence.
    j.reserve_sync(1);record()
    assert blob.read_bytes()==original
    reopened=Journal(tmp_path,'work',child_budget=2,parent_budget=2)
    operation=reopened.operation(token)
    assert (operation['children']['0'] if child else operation)['result']==result
    event=next(e for e in reopened.collect() if e['kind']==('child_terminal' if child else 'terminal'))
    assert event['result']==result
    assert j.path.stat().st_size < 65536
    with pytest.raises(ValueError,match='conflicting'):
        if child:asyncio.run(j.record_child_terminal(token,0,{'status':'failed'},1.0))
        else:j.terminal_sync(token,{'status':'failed'})
    blob.write_bytes(b'{}')
    with pytest.raises(ValueError,match='evidence'):
        reopened.state()
