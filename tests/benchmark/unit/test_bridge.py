import pytest
from sandbox_graph.domain.models import Operation,ScriptRef,canonical_hash
from sandbox_bench.contracts import CaseContract
from tests.benchmark.unit.test_contract_models import sample
from tests.benchmark.unit.test_session import Scenario

@pytest.mark.asyncio
async def test_gateway_persists_binding_and_replays_receipt(tmp_path):
    from sandbox_bench.session import BenchSession
    from sandbox_bench.bridge import BenchGateway
    case=CaseContract.model_validate(sample());scenario=Scenario()
    session=BenchSession(case,{'product':'kuasar','api_url':'http://localhost:1','data_url':'http://localhost:1'},tmp_path,scenario=scenario)
    script=ScriptRef(script_id='benchmark.step',version='1',digest='a'*64)
    gateway=BenchGateway({'g/w':session},script)
    body={'step_id':'N05','case_hash':case.digest()}
    op=Operation(operation_id='op1',input_hash=canonical_hash(body),script_ref=script,scope='g/w',input=body,deadline=99999999999,node_id='N05')
    result=await gateway.submit(op)
    assert result.state=='completed' and len(scenario.calls)==5
    assert (await gateway.reconcile('op1','g/w')).state=='completed'
    assert (await gateway.submit(op)).state=='completed'
    assert len(scenario.calls)==5
    with pytest.raises(PermissionError):await gateway.reconcile('op1','other')
    collection=await gateway.collect('op1','g/w')
    assert collection.last_seq>0
    assert (await gateway.collect('op1','g/w',collection.last_seq)).events==()
