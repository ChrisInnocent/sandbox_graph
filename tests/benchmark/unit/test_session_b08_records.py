import json
from pathlib import Path
import pytest
from sandbox_bench.contracts import CaseContract
from sandbox_bench.session import BenchSession
from sandbox_bench.load import OperationResult
from sandbox_bench.journal import FileJournal
from sandbox_bench.runner import atomic_json, digest
from sandbox_bench.analysis import analyze_case
from tests.benchmark.unit.test_contract_models import sample
from tests.benchmark.unit.test_session import Scenario


async def captured(tmp_path,validation=None,cancelled=False):
    data=sample();data['method_id']='B08.LOCAL';data['completion']['endpoint']='API_COMPLETE'
    config={'api_url':'http://localhost:1','data_url':'http://localhost:1','product':'kuasar',
            'wiring':{'restore':{'timeout_s':3}}}
    session=BenchSession(CaseContract.model_validate(data),config,tmp_path,scenario=Scenario(),clock_domain='c')
    token=session.journal.reserve_sync(0);session.journal.submission_started_sync(token)
    child={'status':'success','source':{'source_id':'snap','kind':'snapshot'},
           'completion':{'observed_at':1,'latency':1}}
    await session.journal.record_child_terminal(token,0,OperationResult('success',value=child),1)
    value={**child,'validation':validation,'validation_interference':True}
    result={'status':'unknown','value':None} if cancelled else {'status':'success','value':value}
    session.journal.terminal_sync(token,result)
    report={'started_at':0,'finished_at':4,'clock_domain':'c','results':{'0':result},
            'events':[{'kind':'completed','request_index':0,'completed_at':4}]}
    atomic_json(tmp_path/'load.json',report)
    return session,report


def restore_receipt(session,status,validation):
    journal=FileJournal(session.folder/'fixture_restore',session.case.work_id+':snapshot-restore',child_budget=1,parent_budget=1)
    binding=digest({'case':session.case.digest(),'source_id':'snap','kind':'snapshot','request_index':0,
                    'environment':session.environment,'timeout_s':3.0})
    journal.receipt('restore:0',binding,{'status':status,'validation':validation,'source_id':'snap',
                                       'request_index':0,'sandbox_id':'restored',
                                       'completion':{'status':'success'} if validation is not None else None})
    return journal


@pytest.mark.asyncio
@pytest.mark.parametrize('success',[True,False])
async def test_b08_parent_validation_attaches_to_matching_capture_only(tmp_path,success):
    session,report=await captured(tmp_path,{'success':success})
    child=list(session._subjects(report))[0][1]
    assert child['validation']['success'] is success
    result=await session._N08()
    assert result['analysis']['qualifications']['post_validation']['status']==('valid' if success else 'invalid')
    report['results']['0']['value']['source']['source_id']='foreign'
    with pytest.raises(ValueError,match='capture'):list(session._subjects(report))


@pytest.mark.asyncio
async def test_b08_cancelled_restore_drain_keeps_capture_window_and_durable_validation(tmp_path):
    session,report=await captured(tmp_path,cancelled=True)
    restore_receipt(session,'valid',True)
    records=session.records()
    assert records['window']['end_s']==1
    assert analyze_case(records)['throughput']['overall_per_s']==1
    child=list(session._subjects(report))[0][1]
    assert child['validation']['success'] is True
    assert child['validation']['restore_receipt_ref'].endswith('/receipts/restore:0')
    assert child['completion']['latency']==1


@pytest.mark.asyncio
async def test_b08_unknown_restore_never_becomes_failed_semantic_proof(tmp_path):
    session,report=await captured(tmp_path,{'success':False,'reason':'assertion_failed'})
    restore_receipt(session,'unknown',None)
    assert list(session._subjects(report))[0][1].get('validation') is None
    assert (await session._N08())['analysis']['qualifications']['post_validation']['status']=='unverified'
