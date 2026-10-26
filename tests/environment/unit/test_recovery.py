from types import SimpleNamespace
import pytest
from tests.environment.unit.test_workflow_handlers import setup
from sandbox_graph.environment.recovery import EnvironmentRecoveryOperations


@pytest.mark.asyncio
async def test_recovery_retains_original_plan_and_current_owner_after_runtime_switch(tmp_path):
    repo,handler,context,_,_=setup(tmp_path)
    with repo.transaction() as tx:
        token=tx.repo.leases['thread'];run=tx.get('runs',context.run_ref)['body']
        original_plan=run['active_plan_ref']
        tx.put('operations','op',{'run_id':context.run_ref,'scope':context.run_ref})
        tx.put('runs',context.run_ref,run|{'active_plan_ref':'some-new-plan'})
    calls=[]
    class Operations:
        async def reconcile(self,*args,**kwargs):calls.append((args,kwargs));return 'original'
    def factory(owner,activation):
        assert owner==token and activation.plan_ref==original_plan
        return SimpleNamespace(gateway=SimpleNamespace(operation_service=Operations()))
    proxy=EnvironmentRecoveryOperations(repo,factory);guard=lambda tx:None
    assert await proxy.reconcile('op',context.run_ref,token,safety_guard=guard)=='original'
    assert calls==[(('op',context.run_ref,token),{'safety_guard':guard})]
    with pytest.raises(PermissionError):await proxy.reconcile('op','foreign',token)
