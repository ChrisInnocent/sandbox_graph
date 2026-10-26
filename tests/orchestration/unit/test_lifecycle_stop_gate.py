import json
from pathlib import Path

import pytest

from sandbox_graph.testing.local_gate import evaluate_cases, freeze_inputs, run_gate


def receipts():
    return {'collected':['test_a.py::test_ok[x]'],'collection_errors':[],
        'phases':[{'nodeid':'test_a.py::test_ok[x]','when':phase,'outcome':'passed','wasxfail':False}
            for phase in ('setup','call','teardown')]}


@pytest.mark.parametrize('mutation', ['missing','variant','duplicate','skip','xfail','teardown','zero','junit','timeout','changed','exit'])
def test_rejects_incomplete_or_changed_evidence(mutation):
    value=receipts();required=['test_a.py::test_ok[x]'];kwargs=dict(returncode=0,junit_count=1,timed_out=False,inputs_unchanged=True)
    if mutation=='missing': value['phases'].pop()
    if mutation=='variant': required.append('test_a.py::test_ok[y]')
    if mutation=='duplicate': value['phases'].append(value['phases'][0])
    if mutation=='skip': value['phases'][1]['outcome']='skipped'
    if mutation=='xfail': value['phases'][1]['wasxfail']=True
    if mutation=='teardown': value['phases'][2]['outcome']='failed'
    if mutation=='zero': required=[];value['collected']=[];value['phases']=[];kwargs['junit_count']=0
    if mutation=='junit': kwargs['junit_count']=None
    if mutation=='timeout': kwargs['timed_out']=True
    if mutation=='changed': kwargs['inputs_unchanged']=False
    if mutation=='exit': kwargs['returncode']=2
    assert evaluate_cases(required,value,**kwargs)['status']=='fail'


def test_complete_stage_set_passes():
    assert evaluate_cases(['test_a.py::test_ok[x]'],receipts(),returncode=0,junit_count=1,
        timed_out=False,inputs_unchanged=True)['status']=='pass'


def test_dirty_and_untracked_input_changes_invalidate_digest(tmp_path):
    (tmp_path/'a.py').write_text('x=1\n')
    before=freeze_inputs(tmp_path,['a.py'])
    (tmp_path/'a.py').write_text('x=2\n')
    assert freeze_inputs(tmp_path,['a.py'])['hash']!=before['hash']


def test_missing_environment_is_blocked_before_subprocess(tmp_path,monkeypatch):
    monkeypatch.delenv('SANDBOX_GRAPH_TEST_POSTGRES_DSN',raising=False)
    result=run_gate(tmp_path,{'profile':'test','paths':['test_a.py'],'required_ids':['test_a.py::test_ok'],
        'requires':['SANDBOX_GRAPH_TEST_POSTGRES_DSN']},tmp_path/'evidence')
    assert result['status']=='blocked'
    assert result['reason']=='missing_environment'


def test_real_pytest_receipts_match_collect_run_and_junit(tmp_path):
    (tmp_path/'test_a.py').write_text('def test_ok():\n    assert 2+2==4\n')
    (tmp_path/'pytest.ini').write_text('[pytest]\n')
    result=run_gate(tmp_path,{'profile':'test','paths':['test_a.py'],'required_ids':['test_a.py::test_ok'],
        'requires':[]},tmp_path/'evidence',config='pytest.ini',timeout=30)
    assert result['status']=='pass',result
    assert json.loads((tmp_path/'evidence'/'run.json').read_text())['phases'][1]['when']=='call'
