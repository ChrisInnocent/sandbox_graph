from dataclasses import replace
from copy import deepcopy
import pytest
from tests.orchestration.conftest import MemoryRepository,FakeClock
from tests.benchmark.unit.test_contract_models import sample
from tests.benchmark.unit.test_session import Scenario
from sandbox_bench.bindings import RegisteredBenchmarks
from sandbox_bench.bridge import BenchGateway
from sandbox_bench.session import BenchSession
from sandbox_graph.domain.models import ScriptRef
from sandbox_graph.graphs.executor import NodeContext
from sandbox_graph.runtime.leases import LeaseService


@pytest.mark.asyncio
async def test_registered_condition_binding_is_durable_scoped_and_partial(tmp_path):
    repo=MemoryRepository(FakeClock())
    token=LeaseService(repo).acquire_thread('t','worker',50)
    gateway=BenchGateway({},ScriptRef(script_id='bench',version='1',digest='a'*64))
    cfg={'product':'kuasar','api_url':'http://localhost:1','data_url':'http://localhost:1'}
    templates={'case-v1':{'case':sample(),'config':cfg}}
    bindings=RegisteredBenchmarks(repo,gateway,templates,tmp_path,
        session_factory=lambda c,cfg,p:BenchSession(c,cfg,p,scenario=Scenario()))
    with repo.transaction() as tx:
        for name,ref in [('work','case-v1'),('missing','not-configured')]:
            tx.put('work_instances',name,{'work_id':name,'run_id':'r','plan_ref':'p','group_id':'g',
                'status':'pending','condition':{'case_template_ref':ref},
                'key':{'method_id':'B01.TEMPLATE'},'contract_ref':sample()['contract_ref']})
    ctx=NodeContext('T2','owner','t','g','r','p','graph','activation','visit',{}, {},
                    token.owner_epoch,token.lease_id)
    result=await bindings.validate_conditions(ctx)
    assert result.continuation=='T4' and result.output['gap_count']==1
    session=bindings.session('work')
    assert session.case.work_id=='work' and session.case.plan_ref=='p'
    assert bindings.session('work') is session
    with repo.transaction() as tx:
        bound=tx.get('measurement_bindings','work')['body']
        assert bound['case_hash']==session.case.digest()
        assert bound['measurement_contract_hash']
        work=tx.get('work_instances','work')['body']
        tx.put('work_instances','repeat2',{**work,'work_id':'repeat2',
               'key':{**work['key'],'repeat_id':2}})
    assert bindings.session('repeat2').case.repeat_id==2
    assert bindings.session('repeat2').case.digest()!=session.case.digest()
    with repo.transaction() as tx:
        assert tx.get('work_instances','missing')['body']['status']=='blocked'
    bindings.templates['case-v1']['config']['api_url']='http://other'
    with pytest.raises(ValueError,match='changed'):bindings.session('work')


def test_measurement_contract_hash_excludes_only_graph_identity():
    from sandbox_bench.contracts import CaseContract
    from sandbox_bench.identity import measurement_contract_hash
    original=CaseContract.model_validate(sample())
    moved=original.model_copy(update={'work_id':'new-work','group_id':'new-group',
                                      'plan_ref':'new-plan','contract_ref':'new-contract'})
    assert measurement_contract_hash(original)==measurement_contract_hash(moved)
    for field,value in [('repeat_id',2),('cache_profile','different'),('product_version','other')]:
        assert measurement_contract_hash(original)!=measurement_contract_hash(original.model_copy(update={field:value}))
    changed=original.model_copy(update={'load':original.load.model_copy(update={'concurrency':1})})
    assert measurement_contract_hash(original)!=measurement_contract_hash(changed)
