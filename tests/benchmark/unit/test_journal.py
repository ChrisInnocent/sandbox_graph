import importlib.util
import pytest


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
