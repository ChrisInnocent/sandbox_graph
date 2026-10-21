"""Actual process death; external effects are a durable synthetic service file."""
import os
from pathlib import Path
import subprocess
import sys
import pytest
from sandbox_bench.journal import FileJournal


@pytest.mark.parametrize('cut', ['intent', 'external_effect', 'child_receipt'])
def test_process_death_never_resets_child_budget_or_reissues_unknown(tmp_path, cut):
    script = r'''
import asyncio, os, sys
from pathlib import Path
from sandbox_bench.journal import FileJournal
from sandbox_bench.load import OperationResult
root=Path(sys.argv[1]);cut=sys.argv[2]
j=FileJournal(root/'ledger','w',child_budget=2,parent_budget=1)
token=j.reserve_sync(0,2);j.submission_started_sync(token)
if cut=='intent':os._exit(71)
with (root/'external-effect').open('x') as stream:
    stream.write('created:child-0');stream.flush();os.fsync(stream.fileno())
j.own('sandbox','child-0',{'environment':'e'})
if cut=='external_effect':os._exit(72)
asyncio.run(j.record_child_terminal(token,0,OperationResult('success',value={'sandbox_id':'child-0'}),1.0))
os._exit(73)
'''
    result = subprocess.run([sys.executable,'-c',script,str(tmp_path),cut],
                            cwd=Path(__file__).resolve().parents[3], timeout=10)
    assert result.returncode in {71,72,73}
    journal = FileJournal(tmp_path/'ledger','w',child_budget=2,parent_budget=1)
    token = journal.reserve_sync(0,2)
    assert journal.state()['child_reserved']==2
    with pytest.raises(ValueError,match='already submitted'):journal.submission_started_sync(token)
    with pytest.raises(ValueError,match='budget'):journal.reserve_sync(1,1)
    if cut != 'intent':
        assert (tmp_path/'external-effect').read_text()=='created:child-0'
        assert journal.owned('e')[0]['resource_id']=='child-0'
        with pytest.raises(ValueError,match='environment'):journal.owned('foreign')
    if cut=='child_receipt':
        assert journal.operation(token)['children']['0']['result']['status']=='success'
    assert journal.operation(token)['status']=='submission_started'
