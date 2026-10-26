import pytest
from sandbox_graph.domain.models import ScriptRef, canonical_hash
from sandbox_graph.environment.contracts import ActionStep, RepairPlan
from sandbox_graph.environment.execution import EnvironmentRunnerGateway
from sandbox_graph.runtime.runner import RunnerStatus


class Service:
    def __init__(self, status): self.status=status; self.calls=[]
    async def submit(self, operation, token):
        self.calls.append(operation)
        return self.status
    async def reconcile(self, operation_id, scope, token):
        return self.status


def plan_step():
    data={'unit':'cubelet.service'}
    step=ActionStep(step_id='step-1',action_ref='cube.service.start.v1',node_id='node-1',input=data,
        input_hash=canonical_hash({'action':'cube.service.start.v1','input':data}),reconcile_rule='check-unit',timeout_seconds=30,ownership='environment:env')
    plan=RepairPlan(plan_hash='a'*64,report_ref='artifact://env/report',declaration_hash='b'*64,steps=(step,),
        conflict_domains=('host:node-1',),revision=1,deadline='2026-10-21T01:00:00Z',repair_round_budget=1)
    return plan,step


@pytest.mark.asyncio
async def test_gateway_binds_step_to_registered_operation_and_returns_receipt():
    plan,step=plan_step(); ref=ScriptRef(script_id='cube.service.start',version='1',digest='c'*64)
    service=Service(RunnerStatus(state='completed',task_handle='remote-1',authoritative=True,artifact_refs=('artifact://receipt/1',)))
    gateway=EnvironmentRunnerGateway(service,action_scripts={step.action_ref:ref})
    receipt=await gateway.submit_step(plan=plan,step=step,scope='env/group',run_id='run-1',deadline=1_798_000_000,
        dispatch_generation=3,token=object())
    operation=service.calls[0]
    assert operation.input['plan_hash']==plan.plan_hash and operation.input['step']['step_id']==step.step_id
    assert receipt.effect_certainty=='confirmed' and receipt.result=='completed'
    assert receipt.input_hash==operation.input_hash


@pytest.mark.asyncio
async def test_gateway_keeps_unknown_as_unknown_and_never_fabricates_retry():
    plan,step=plan_step(); ref=ScriptRef(script_id='cube.service.start',version='1',digest='c'*64)
    service=Service(RunnerStatus(state='unknown',authoritative=False,reason='response lost'))
    gateway=EnvironmentRunnerGateway(service,action_scripts={step.action_ref:ref})
    receipt=await gateway.submit_step(plan=plan,step=step,scope='env/group',run_id='run-1',deadline=1_798_000_000,
        dispatch_generation=3,token=object())
    assert receipt.result=='unknown' and receipt.effect_certainty=='unknown'
    assert service.calls[0].operation_id==f'environment:{plan.plan_hash}:{step.step_id}'


class Tx:
    def __init__(self, records): self.records=records
    def assert_fence(self, token): pass
    def get(self, table, key):
        body=self.records.get((table,key))
        return {"body":body} if body is not None else None
    def put(self, table, key, body): self.records[(table,key)]=body
    def __enter__(self): return self
    def __exit__(self,*args): pass


class Repository:
    def __init__(self): self.records={}
    def transaction(self): return Tx(self.records)


@pytest.mark.asyncio
async def test_query_missing_durable_operation_is_unknown_and_not_resubmitted():
    plan,step=plan_step(); ref=ScriptRef(script_id='cube.service.start',version='1',digest='c'*64)
    service=Service(RunnerStatus(state='completed',authoritative=True)); service.repository=Repository()
    service.repository.records[('resource_leases','host:node-1')]={'scope':'env/group','status':'reserved'}
    gateway=EnvironmentRunnerGateway(service,action_scripts={step.action_ref:ref})
    receipt=await gateway.query_step(plan=plan,step=step,scope='env/group',token=object())
    assert receipt.result=='unknown' and receipt.effect_certainty=='unknown'
    assert service.repository.records[('resource_leases','host:node-1')]['status']=='quarantine'
    assert not service.calls

@pytest.mark.asyncio
async def test_gateway_uses_published_plan_artifact_and_rejects_rebinding():
    plan,step=plan_step();ref=ScriptRef(script_id='cube.service.start',version='1',digest='c'*64)
    service=Service(RunnerStatus(state='completed',authoritative=True))
    gateway=EnvironmentRunnerGateway(service,action_scripts={step.action_ref:ref})
    gateway.bind_plan(plan,'artifact://env/'+('d'*64))
    receipt=await gateway.submit_step(plan=plan,step=step,scope='env',run_id='run',deadline=2000.,dispatch_generation=1,token=object())
    assert receipt.plan_ref==service.calls[0].input['plan_ref']=='artifact://env/'+('d'*64)
    with pytest.raises(ValueError,match='changed'):gateway.bind_plan(plan,'artifact://env/'+('e'*64))
