import pytest
from sandbox_graph.domain.models import NodeResult,RunRecord
from sandbox_graph.services.agent_lifecycle import LifecycleService
from sandbox_graph.services.lifecycle_safety import LifecycleSafetyService
from sandbox_graph.persistence.repository import Conflict
from tests.orchestration.unit.test_lifecycle_models import policy_data
from sandbox_graph.domain.agent_lifecycle import RepairPolicy


def seeded(repository,node,scope='group/a',run='run/a'):
    value=policy_data();value.update(deadline=1100.0,safety_budget=value['safety_budget']|{'deadline':1200.0})
    service=LifecycleService(repository);service.register_policy('policy/a',RepairPolicy.model_validate(value))
    with repository.transaction() as tx:
        token=tx.acquire_thread(run+':thread','worker',100)
        body=RunRecord(run_id=run,root_thread_id=run+':thread',graph_ref='g:1',contract_ref='contract/a',active_plan_ref='plan/a',auth_ref='auth/a',lineage_id=run)
        tx.put('runs',run,body.model_dump(mode='json'))
        if scope!=run:tx.put('groups',run+':group',{'run_id':run,'group_id':scope,'plan_ref':'plan/a'})
        identity={'run_ref':run,'scope':scope,'node_id':node,'thread_id':token.thread_id,
            'activation_id':run+':activation','plan_ref':'plan/a','graph_ref':'g:1','input_refs':{}}
        ref=run+':receipt'
        tx.put('receipts',ref,{'identity':identity,'result':NodeResult(execution_status='failed').model_dump(mode='json')})
        tx.put('activations',identity['activation_id'],{'status':'complete','record':{'activation_id':identity['activation_id'],
            'run_id':run,'scope':scope,'plan_ref':'plan/a','graph_ref':'g:1'},'result':{'result_refs':{node:ref}}})
    life=service.observe_failure(ref,policy_ref='policy/a',token=token)
    return life,token


@pytest.mark.parametrize('node,scope',[('E1','group/a'),('C2','group/a'),('G0','group/a'),('R1','run/a'),('A3','run/a'),('F1','run/a')])
def test_nonwork_scope_closure_uses_real_activation_and_no_measurement_rows(repository,node,scope):
    life,token=seeded(repository,node,scope)
    safety=LifecycleSafetyService(repository);safety.acquire_work(scope,token,purpose='diagnosis')
    ref=safety.produce_disposition(scope,token,ttl=10)
    assert safety.verify_disposition(ref,scope,token).stopped
    with repository.transaction() as tx:assert not tx.scan('work_instances')
    safety.release_work(scope,token)


def test_unknown_install_operation_blocks_scope_closure_and_cross_run_proof(repository):
    life,token=seeded(repository,'C2')
    safety=LifecycleSafetyService(repository);safety.acquire_work('group/a',token,purpose='diagnosis')
    with repository.transaction() as tx:
        tx.put('operations','original-install',{'run_id':'run/a','scope':'group/a','phase':'uncertain','status':{'authoritative':False}})
    ref=safety.produce_disposition('group/a',token,ttl=10)
    with pytest.raises(Conflict):safety.verify_disposition(ref,'group/a',token)
    _,other=seeded(repository,'C2',run='run/b')
    safety.acquire_work('group/a',other,purpose='diagnosis')
    with pytest.raises(Conflict):safety.verify_disposition(ref,'group/a',other)

@pytest.mark.asyncio
@pytest.mark.parametrize('node,scope',[('E2','group/a'),('C2','group/a'),('R1','run/a'),('A3','run/a'),('F1','run/a')])
async def test_nonwork_supervisor_runs_durable_diagnosis_and_explicit_unavailable_without_measurement(repository,node,scope):
    from sandbox_graph.runtime.lifecycle_supervisor import LifecycleSupervisor
    from sandbox_graph.runtime.worker import Worker,WorkerPolicy
    life,token=seeded(repository,node,scope)
    limits={'repair_attempts':2,'diagnosis_calls':2,'candidate_revisions':2,'tokens':30,'child_jobs':2,'observations':2,'cost':1}
    safety={'stop_calls':2,'query_calls':2,'cleanup_calls':2,'cost':1}
    binding={'binding_ref':'binding/a','run_ref':'run/a','scope_ref':scope,'node_id':node,'policy_ref':'policy/a',
        'authorization_ref':'auth/a','principal':'operator','limits':limits,'lineage_limits':limits,
        'deadline':1100.,'lineage_deadline':1100.,'safety_limits':safety,'lineage_safety_limits':safety,
        'safety_deadline':1200.,'lineage_safety_deadline':1200.,'cost_unit':'USD'}
    supervisor=LifecycleSupervisor(repository,bindings=[binding],lease_seconds=20)
    with repository.transaction() as tx:
        supervisor._initialize_in(tx,supervisor._binding('run/a',scope,node),life.key.lifecycle_id)
        tx.put('dispatch_gates','run/a',{'open':True,'generation':1})
        tx.release_thread(token)
    worker=Worker(repository,{'lifecycle':supervisor},policy=WorkerPolicy(claim_seconds=20,job_seconds=10,poll_seconds=.1))
    for _ in range(6):await worker.run_once()
    with repository.transaction() as tx:
        assert LifecycleService.load_in(tx,life.key.lifecycle_id).phase=='waiting_decision'
        assert not tx.scan('work_instances') and not tx.scan('operations')
        assert len([row for row in tx.scan('jobs') if row['body']['job_kind']=='lifecycle'])==2


def test_environment_bootstrap_exposes_registered_lifecycle_and_candidate_safety(repository):
    from types import SimpleNamespace
    from sandbox_graph.runtime.bootstrap import build_job_handlers
    handlers=build_job_handlers(repository,graph_ref='environment:1',saver_factory=lambda _:None,
        node_service_factory=lambda *_:{},review_service=None,deadline=1100.,lease_seconds=30.,segment_seconds=20.,
        lifecycle_bindings=[],evolution=SimpleNamespace(safety=object()))
    assert {'environment','resume','lifecycle','candidate','candidate_cleanup'}<=set(handlers)
