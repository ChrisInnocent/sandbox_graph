from datetime import datetime,timezone
import pytest
from sandbox_graph.environment.verification import ProductVerification
from tests.environment.unit.test_canary import prepare,Adapter

class Probe:
    def __init__(self,runner):self.runner=runner
    async def request(self,node,action,data):
        assert action=='probe.configuration.v1'
        return {'components':{'server':{'status':'satisfied','evidence_level':'startup_bound','sha256':'a'*64,'pid':42,'start_ticks':9,'values':{}}}}
    def commit(self,value):
        import json
        ref=self.runner.store.commit([json.dumps(value,sort_keys=True).encode()],scope=self.runner.token.thread_id,
            media_type='application/json',producer='environment-probe.v1')
        return 'artifact://'+ref.uri

@pytest.mark.asyncio
async def test_missing_canary_is_a_finite_repair_requirement(tmp_path):
    repo,runner,op,_=prepare(tmp_path,Adapter())
    with repo.transaction() as tx:
        from sandbox_graph.environment.contracts import EnvironmentDeclaration
        d=EnvironmentDeclaration.model_validate(tx.scan('environment_declarations')[-1]['body'])
    verifier=ProductVerification(probe=Probe(runner),repository=repo,scope=op.scope,store=runner.store)
    result=await verifier.check(d,d.nodes[0],fresh=True,boot_id='boot')
    assert any(x.status=='missing' and x.repair_code=='CANARY_MISSING' for x in result)

@pytest.mark.asyncio
async def test_canary_requires_current_boot_raw_proofs_and_completed_cleanup(tmp_path):
    repo,runner,op,_=prepare(tmp_path,Adapter());await runner.submit(op)
    with repo.transaction() as tx:
        from sandbox_graph.environment.contracts import EnvironmentDeclaration
        d=EnvironmentDeclaration.model_validate(tx.get('environment_declarations',runner._request(op).declaration_hash)['body'])
    verifier=ProductVerification(probe=Probe(runner),repository=repo,scope=op.scope,store=runner.store)
    assert all(x.status in {'satisfied','not_applicable'} for x in await verifier.check(d,d.nodes[0],fresh=True,boot_id='boot'))
    changed=await verifier.check(d,d.nodes[0],fresh=True,boot_id='new-boot')
    assert any(x.status!='satisfied' and x.check_id=='functional_readiness' for x in changed)

@pytest.mark.asyncio
async def test_unknown_original_canary_never_becomes_new_repair(tmp_path):
    repo,runner,op,_=prepare(tmp_path,Adapter(lost=True));await runner.submit(op)
    with repo.transaction() as tx:
        from sandbox_graph.environment.contracts import EnvironmentDeclaration
        d=EnvironmentDeclaration.model_validate(tx.get('environment_declarations',runner._request(op).declaration_hash)['body'])
    result=await ProductVerification(probe=Probe(runner),repository=repo,scope=op.scope,store=runner.store).check(d,d.nodes[0],fresh=True,boot_id='boot')
    functional=next(x for x in result if x.check_id=='functional_readiness')
    assert functional.status=='unknown' and functional.repair_code is None
