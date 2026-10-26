"""ALI-01 controlled file effects; actual StateGraph/Worker/services, no remote backend."""
from copy import deepcopy
from dataclasses import dataclass, replace
from collections.abc import Mapping
from math import isfinite

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from sandbox_graph.catalog.registry import NODE_GROUPS
from sandbox_graph.catalog.repair_actions import (
    NodeExecutorRoute, RepairActionCatalog, RepairActionDefinition, RepairApplicabilityRule, standard_repair_catalog)
from sandbox_graph.domain.agent_lifecycle import ResourceDispositionReceipt
from sandbox_graph.domain.models import DeliveryManifest, ErrorRecord, NodeResult
from sandbox_graph.persistence.repository import Conflict
from sandbox_graph.runtime.runner import RunnerStatus
from sandbox_graph.services.lifecycle_safety import LifecycleSafetyService
from sandbox_graph.graphs.factory import build_work
from sandbox_graph.runtime.lifecycle_supervisor import LifecycleSupervisor
from sandbox_graph.runtime.tasks import JobSpec, TaskBackend
from sandbox_graph.runtime.worker import GraphJobHandler, RemedyJobHandler, Worker, WorkerPolicy
from tests.orchestration.graphs.test_work_remediation import drain
from tests.orchestration.unit.test_lifecycle_service import setup_lifecycle


@dataclass(frozen=True)
class FileRepair(RepairActionDefinition):
    target: object = None
    expected: bytes = b''

    def postcheck(self, result, review, **facts):
        # Independent observation of the effect, plus the production operation proof.
        reconcile=standard_repair_catalog().resolve('case.reconcile','1','N01')
        return (self.target.read_bytes()==self.expected
            and reconcile.postcheck(result,review,**facts))


@dataclass(frozen=True)
class N02FileRepair(FileRepair):
    """Test-only fixture contract; never delegates N02 acceptance to N01."""

    def postcheck(self, result, review, *, disposition=None, checked_at=None,
                  operation_statuses=None, collection=None):
        return (self.target.read_bytes()==self.expected
            and result.execution_status=='succeeded' and result.error is None
            and result.review_status!='rejected' and review is not None
            and self.node_id==review.node_id=='N02' and review.decision=='accept'
            and isinstance(disposition,ResourceDispositionReceipt)
            and review.owner_agent_id==f'agent:work:{disposition.work_ref}'
            and disposition.producer=='lifecycle_safety' and disposition.producer_version=='1'
            and type(checked_at) in {int,float} and isfinite(checked_at)
            and disposition.observed_at<=checked_at<disposition.expires_at
            and disposition.stopped and disposition.admission_status=='closed'
            and disposition.sampler_status in {'stopped','not_started'}
            and not disposition.residual_resources and not disposition.isolation_domains
            and result.operation_refs==(f'op/{disposition.work_ref}/N02',)
            and set(result.operation_refs).issubset(disposition.operation_refs)
            and isinstance(operation_statuses,Mapping)
            and set(operation_statuses)==set(result.operation_refs)
            and all(isinstance(status,RunnerStatus) and status.authoritative is True
                and status.state=='completed' for status in operation_statuses.values()))


def strategy_assembly(repository, tmp_path, *, source_node='N01', b_postcheck=True,
                      reverse_policy=False, a_postcheck_failure=False, recovery_postcheck=True):
    service, token=setup_lifecycle(repository)
    raw=tmp_path/'raw.bin'; raw.write_bytes(b'original raw\x00\xff')
    target=tmp_path/'fixture'; target.write_bytes(b'broken')
    other_target=tmp_path/'fixture-b'; other_target.write_bytes(b'healthy')
    other_raw=tmp_path/'raw-b.bin'; other_raw.write_bytes(b'other environment raw')
    targets={'work/a':target,'work/b':other_target}
    raw_files={'work/a':raw,'work/b':other_raw}
    base=standard_repair_catalog().resolve(
        'case.reconcile' if source_node=='N01' else 'fixture.repair','1',source_node)
    base=replace(base,availability='available',executor_route=NodeExecutorRoute(source_node),
        unavailable_reason=None,verification_status='locally_verified')
    repair_type=FileRepair if source_node=='N01' else N02FileRepair
    definitions=tuple(repair_type(**{name:getattr(base,name) for name in base.__dataclass_fields__},
        target=target,expected=b'healthy') for _ in range(2))
    a=replace(definitions[0],action_id='test.probe',postcheck_ref='test.probe-file@1')
    b=replace(definitions[1],action_id='test.restore',postcheck_ref='test.restore-file@1')
    b_rule={'predicate':'postcheck_rejected','preceding_postcheck_ref':a.postcheck_ref} if a_postcheck_failure else {
        'predicate':'repair_failure','error_codes':('PROBE_INEFFECTIVE',)}
    rules=(RepairApplicabilityRule(schema_version=1,rule_ref='test.probe-rule@1',node_id=source_node,
        action_ref=a.ref,predicate='source_error',error_codes=('FIXTURE_BROKEN',)),
        RepairApplicabilityRule(schema_version=1,rule_ref='test.restore-rule@1',node_id=source_node,
        action_ref=b.ref,preceding_action_ref=a.ref,**b_rule))
    catalog=RepairActionCatalog((a,b),(),rules=rules)
    refs=[b.ref,a.ref] if reverse_policy else [a.ref,b.ref]
    with repository.transaction() as tx:
        tx.release_thread(token)
        tx.delete('receipts','receipt/a'); tx.delete('node_reviews','receipt/a')
        policy=tx.get('repair_policies','policy/a')['body']
        tx.put('repair_policies','policy/runtime',policy | {'actions':refs,'max_same_failure':1,
            'max_repair_attempts':2,'child_job_limit':9})
        tx.put('contracts','contract/a',{'assertions':['owned-operation','fixture-file']},immutable=True)
        work=tx.get('work_instances','work/a')['body']
        tx.put('work_instances','work/b',work | {'work_id':'work/b','group_id':'group/b'})
        tx.put('dispatch_gates','run/a',{'open':True,'generation':3})
        tx.put('repair_authorizations','auth/runtime',{'principal':'operator/a','run_ref':'run/a',
            'scope_ref':'work/a','plan_ref':'plan/a','graph_ref':'graph/a','contract_ref':'contract/a',
            'work_revision':1,'actions':refs,'permissions':list(base.permissions),
            'revoked':False,'expires_at':1100.0})
    limits={'repair_attempts':2,'diagnosis_calls':2,'candidate_revisions':2,'tokens':100,
        'child_jobs':9,'observations':2,'cost':100}
    safety={'stop_calls':2,'query_calls':2,'cleanup_calls':2,'cost':10}
    binding={'binding_ref':'binding/a','run_ref':'run/a','work_ref':'work/a','node_id':source_node,
        'policy_ref':'policy/runtime','authorization_ref':'auth/runtime','principal':'operator/a',
        'limits':limits,'lineage_limits':limits,'deadline':1100.0,'lineage_deadline':1100.0,
        'safety_limits':safety,'lineage_safety_limits':safety,'safety_deadline':1150.0,
        'lineage_safety_deadline':1150.0,'cost_unit':'test_units'}
    supervisor=LifecycleSupervisor(repository,bindings=[binding],lease_seconds=30,repair_catalog=catalog)
    seen=[]; effects=[]
    def owned_operation(context):
        operation='op/' + context.scope + '/' + context.node_id
        with repository.transaction() as tx:
            if not tx.get('operations',operation):
                tx.put('operations',operation,{'scope':context.scope,'run_id':context.run_ref,
                    'thread_id':context.thread_id,'request':{'node_id':context.node_id},'phase':'terminal',
                    'status':{'state':'completed','authoritative':True}})
        return operation
    async def source(context):
        operation=owned_operation(context)
        with repository.transaction() as tx:
            job=next((r['body'] for r in tx.scan('jobs') if r['body']['activation_id']==context.activation_id),{})
            order=tx.get('repair_orders',job.get('payload',{}).get('repair_order_ref',''))
        if order:
            action=order['body']['action_ref']
            if action==a.ref:
                target.write_bytes(b'probe-only')
                effects.append('probe')
                if a_postcheck_failure:
                    return NodeResult(execution_status='succeeded',operation_refs=(operation,))
                return NodeResult(execution_status='failed',operation_refs=(operation,),
                    error=ErrorRecord(code='PROBE_INEFFECTIVE',observed_at=1000.0,retry_class='bounded'))
            if action==b.ref:
                target.write_bytes(b'healthy' if b_postcheck else b'still-broken')
                effects.append('restore')
                return NodeResult(execution_status='succeeded',operation_refs=(operation,))
        if targets[context.scope].read_bytes()!=b'healthy':
            return NodeResult(execution_status='failed',operation_refs=(operation,),
                error=ErrorRecord(code='FIXTURE_BROKEN',observed_at=1000.0))
        return NodeResult(execution_status='succeeded',operation_refs=(operation,))
    async def n01(context):
        return NodeResult(execution_status='succeeded',operation_refs=(owned_operation(context),))
    async def validate(context):
        if context.node_id in {'N02','N03'}:
            assert targets[context.scope].read_bytes()==b'healthy'
        return NodeResult(execution_status='succeeded',output={'validated':context.node_id})
    async def measurement(context):
        assert context.node_id in {'N04','N05','N06'}
        assert targets[context.scope].read_bytes()==b'healthy'
        return NodeResult(execution_status='succeeded',data_status='valid',performance_status='fail',artifact_refs=(str(raw_files[context.scope]),))
    async def seal(context):
        selected=raw_files[context.scope]
        assert selected.read_bytes()==(b'original raw\x00\xff' if context.scope=='work/a' else b'other environment raw')
        return NodeResult(execution_status='succeeded',data_status='valid',artifact_refs=(str(selected),))
    async def cleanup(context):
        return NodeResult(execution_status='succeeded',cleanup_status='complete',
            operation_refs=tuple('op/'+context.scope+'/'+node for node in
                (('N01',) if source_node=='N01' else ('N01','N02'))))
    async def delivery(context):
        if not recovery_postcheck and context.activation_id.startswith('recovery-work:'):
            target.write_bytes(b'changed-after-source-execution')
        manifest=DeliveryManifest(delivery_id=context.activation_id,version=1,scope=context.scope,
            plan_ref=context.plan_ref,contract_ref='contract/a',work_refs=(context.scope,),evidence=(),
            result=NodeResult(execution_status='succeeded',cleanup_status='complete'))
        return NodeResult(execution_status='succeeded',output={'delivery_manifest':manifest.model_dump(mode='json')})
    node_handlers={'N00':validate,'N01':n01,'N02':validate,'N03':validate,
        'N04':measurement,'N05':measurement,'N06':measurement,'N07':seal,'N08':validate,
        'N09':cleanup,'N10':delivery}
    node_handlers[source_node]=source
    def factory(token,activation):
        async def observe(context):
            with repository.transaction() as tx:
                owner=tx.get('work_execution_leases',context.scope)['body']['owner']
                assert owner=={'thread_id':token.thread_id,'owner_epoch':token.owner_epoch,
                    'lease_id':token.lease_id}
                competing=tx.acquire_thread('competing/'+context.activation_id,'competitor',30)
                with pytest.raises(Conflict,match='work already executing'):
                    LifecycleSafetyService.acquire_work_in(tx,context.scope,competing)
                tx.release_thread(competing)
            seen.append((context.scope,context.activation_id,context.node_id,dict(context.input_refs)))
            return await node_handlers[context.node_id](context)
        return {node:observe for node in NODE_GROUPS['work']}
    graph=GraphJobHandler(repository,build_work,lambda token:InMemorySaver(),factory,
        lease_seconds=30,segment_seconds=20,repair_catalog=catalog)
    remedy=RemedyJobHandler(repository,factory,lease_seconds=30,segment_seconds=20,repair_catalog=catalog)
    worker=Worker(repository,{'work':graph,'remedy':remedy,'lifecycle':supervisor},
        policy=WorkerPolicy(claim_seconds=30,job_seconds=20,poll_seconds=1))
    TaskBackend(repository).enqueue(JobSpec(job_id='original/job',job_kind='work',scope_ref='work/a',
        thread_id='original/thread',activation_id='original/a',dedupe_key='original/job',due_at=1000.0,
        deadline=1100.0,run_id='run/a',dispatch_generation=3,
        payload={'plan_ref':'plan/a','graph_ref':'graph/a','fixture':'fixture/a'}))
    return worker, seen, effects, raw


@pytest.mark.asyncio
@pytest.mark.parametrize('source_node',['N01','N02'])
@pytest.mark.parametrize('reverse_policy',[False,True])
async def test_failure_evidence_selects_different_last_strategy_then_real_dependency_reentry(repository,clock,tmp_path,reverse_policy,source_node):
    worker,seen,effects,raw=strategy_assembly(repository,tmp_path,reverse_policy=reverse_policy,
        source_node=source_node)
    assert await worker.run_once()
    original_nodes=['N00','N01','N07','N08','N09','N10'] if source_node=='N01' else [
        'N00','N01','N02','N07','N08','N09','N10']
    assert [node for _,activation,node,_ in seen if activation=='original/a']==original_nodes
    with repository.transaction() as tx:
        frozen={r['key']:deepcopy(r) for r in tx.scan('receipts')}
        failed=[r for r in frozen.values() if r['body']['result']['execution_status']=='failed']
        assert len(failed)==1 and failed[0]['body']['identity']['node_id']==source_node
        assert failed[0]['body']['result']['error']['code']=='FIXTURE_BROKEN'
        assert tx.get('jobs','original/job')['body']['status']=='failed'
        facts=tx.get('work_safety_facts','work/a')['body']
        assert facts['attempt_closed'] is True
        assert set(facts['receipt_refs'])=={'N07','N09','N10'}
        assert tx.get('work_execution_leases','work/a') is None
        assert not tx.scan('agent_lifecycles')  # Strict closure precedes supervision.
    await drain(worker)
    with repository.transaction() as tx:
        life_before=deepcopy(tx.scan('agent_lifecycles')[0]['body'])
        assert life_before['phase']=='backoff'
        assert life_before['key']['node_id']==source_node
        history={(kind,r['key']):deepcopy(r) for kind in ('receipts','node_reviews','repair_orders',
            'repair_outcomes','repair_failure_fingerprints','repair_postchecks','repair_inputs',
            'lifecycle_decisions','repair_strategy_selections') for r in tx.scan(kind)}
        first_outcome=tx.get('repair_outcomes',life_before['attempt_refs'][0])['body']
        first_receipt=tx.get('receipts',first_outcome['return_result']['output']['receipt_ref'])['body']
        assert first_receipt['identity']['node_id']==source_node
        assert first_receipt['result']['execution_status']=='failed'
        assert first_receipt['result']['error']['code']=='PROBE_INEFFECTIVE'
        for bounds in tx.scan('lifecycle_budgets'):
            assert bounds['body']['spent']['repair_attempts']==1
            assert bounds['body']['limits']['repair_attempts']==2
    assert effects==['probe']
    assert (tmp_path/'fixture').read_bytes()==b'probe-only'
    TaskBackend(repository).enqueue(JobSpec(job_id='other/job',job_kind='work',scope_ref='work/b',
        thread_id='other/thread',activation_id='other/a',dedupe_key='other/job',due_at=1000.0,
        deadline=1100.0,run_id='run/a',dispatch_generation=3,
        payload={'plan_ref':'plan/a','graph_ref':'graph/a','fixture':'fixture/b'}))
    await drain(worker)
    with repository.transaction() as tx:
        assert tx.get('jobs','other/job')['body']['status']=='complete'
        assert tx.scan('agent_lifecycles')[0]['body']==life_before
        assert tx.get('work_instances','work/b')['body']['group_id']=='group/b'
        assert len(tx.scan('agent_lifecycles'))==1
    assert (tmp_path/'fixture').read_bytes()==b'probe-only'
    assert (tmp_path/'fixture-b').read_bytes()==b'healthy'
    assert (tmp_path/'raw-b.bin').read_bytes()==b'other environment raw'
    clock.advance(1.0)
    await drain(worker)
    with repository.transaction() as tx:
        life=tx.scan('agent_lifecycles')[0]['body']
        assert life['phase']=='fulfilled'
        assert life['key']['lifecycle_id']==life_before['key']['lifecycle_id']
        assert life['source_receipt']==failed[0]['key']
        assert life['key']['node_id']==source_node
        assert [e['kind'] for e in life['events']]==['disposition_confirmed','known_action','claimed',
            'postcheck_fail_with_budget','timer_due','known_action','claimed','postcheck_pass','node_accepted']
        orders=[tx.get('repair_orders',ref)['body'] for ref in life['attempt_refs']]
        assert [o['action_ref'] for o in orders]==['test.probe@1','test.restore@1']
        assert [tx.get('repair_outcomes',o['order_ref'])['body']['postcheck_status'] for o in orders]==['rejected','accepted']
        decisions=sorted((r['body'] for r in tx.scan('lifecycle_decisions')),key=lambda d:d['expected_revision'])
        failed_receipt=tx.get('repair_outcomes',orders[0]['order_ref'])['body']['return_result']['output']['receipt_ref']
        assert decisions[1]['evidence_refs']==[life['source_receipt'],failed_receipt]
        selected=tx.get('repair_strategy_selections',decisions[1]['decision_ref'])['body']
        assert selected['rule']['rule_ref']=='test.restore-rule@1'
        assert selected['rule']['node_id']==source_node
        assert selected['rule']['predicate']=='repair_failure'
        manifest=tx.get('repair_inputs',orders[1]['input_manifest_ref'])['body']['manifest']
        assert manifest['source_receipt']==life['source_receipt']
        assert manifest['evidence_refs']==[life['source_receipt'],failed_receipt]
        assert manifest['input_refs']=={'fixture':'fixture/a'}
        assert selected['issue_fingerprint']==life['issue_fingerprint']
        assert len(tx.scan('repair_failure_fingerprints'))==1
        plan=tx.scan('recovery_plans')[0]['body']
        assert plan['revalidate_nodes']==['N01','N02','N03']
        recovery=next(r['body'] for r in tx.scan('jobs') if r['key'].startswith('recovery-work:'))
        acceptance=recovery['result']['graph_result']['recovery_acceptance']
        assert acceptance['accepted'] is True
        assert acceptance['source_node']==source_node
        assert acceptance['source_receipt']==life['source_receipt']
        assert acceptance['postcheck_ref']=='test.restore-file@1'
        assert acceptance['execution_review_accepted'] is True
        assert acceptance['reason']=='node_postcheck_accepted'
        source_row=tx.get('receipts',acceptance['receipt_ref'])['body']
        assert source_row['identity']['node_id']==source_node
        assert tx.get('node_reviews',acceptance['receipt_ref'])['body']['node_id']==source_node
        operation=source_row['result']['operation_refs'][0]
        assert tx.get('operations',operation)['body']['request']['node_id']==source_node
        assert tx.get('operations',operation)['body']['scope']=='work/a'
        assert recovery['result']['graph_result']['recovery_dependency_failures']=={}
        for ref,value in frozen.items():
            assert tx.get('receipts',ref)==value
        for (kind,ref),value in history.items():
            assert tx.get(kind,ref)==value
        for bounds in tx.scan('lifecycle_budgets'):
            assert bounds['body']['spent']['repair_attempts']==2
            assert bounds['body']['spent']['child_jobs']==9
            assert bounds['body']['spent']['diagnosis_calls']==0
            assert not any(bounds['body']['reserved'].values())
        assert not tx.scan('agent_team_tasks') and not tx.scan('lifecycle_blockers')
    assert effects==['probe','restore']
    recovery_nodes=[node for scope,activation,node,inputs in seen if activation.startswith('recovery-work:')]
    assert recovery_nodes==['N01','N02','N03','N04','N05','N06','N07','N08','N09','N10']
    assert [(scope,node) for scope,_,node,_ in seen]==(
        [('work/a',node) for node in original_nodes]+[('work/a',source_node)]
        +[('work/b',node) for node in ['N00','N01','N02','N03','N04','N05','N06','N07','N08','N09','N10']]
        +[('work/a',source_node)]
        +[('work/a',node) for node in recovery_nodes])
    assert all(inputs=={'fixture':'fixture/a' if scope=='work/a' else 'fixture/b'} for scope,_,_,inputs in seen)
    assert raw.read_bytes()==b'original raw\x00\xff'
    assert (tmp_path/'fixture-b').read_bytes()==b'healthy'
    assert (tmp_path/'raw-b.bin').read_bytes()==b'other environment raw'
    before=list(seen)
    await drain(worker)
    assert seen==before and effects==['probe','restore']


@pytest.mark.asyncio
@pytest.mark.parametrize('source_node',['N01','N02'])
async def test_independent_b_postcheck_can_reject_exit_success_without_recovery(repository,clock,tmp_path,source_node):
    worker,_,effects,_=strategy_assembly(repository,tmp_path,b_postcheck=False,source_node=source_node)
    await drain(worker)
    clock.advance(1.0)
    await drain(worker)
    with repository.transaction() as tx:
        assert tx.scan('agent_lifecycles')[0]['body']['phase']=='blocked'
        assert not tx.scan('recovery_plans')
        life=tx.scan('agent_lifecycles')[0]['body']
        outcomes=[tx.get('repair_outcomes',ref)['body'] for ref in life['attempt_refs']]
        assert [outcome['postcheck_status'] for outcome in outcomes]==['rejected','rejected']
        b_receipt=tx.get('receipts',outcomes[1]['return_result']['output']['receipt_ref'])['body']
        assert b_receipt['identity']['node_id']==source_node
        assert b_receipt['result']['execution_status']=='succeeded'
        assert b_receipt['result']['error'] is None
        assert tx.scan('agent_lifecycles')[0]['body']['key']['node_id']==source_node
    assert (tmp_path/'fixture').read_bytes()==b'still-broken'
    assert effects==['probe','restore']


@pytest.mark.asyncio
async def test_successful_a_execution_with_rejected_postcheck_selects_b_in_original_spare_budget(repository,tmp_path):
    worker,seen,effects,raw=strategy_assembly(repository,tmp_path,a_postcheck_failure=True)
    assert await worker.run_once()
    with repository.transaction() as tx:
        original_receipts={r['key']:deepcopy(r) for r in tx.scan('receipts')}
    for _ in range(10):
        await worker.run_once()
        with repository.transaction() as tx:
            if tx.scan('repair_outcomes'):
                break
    with repository.transaction() as tx:
        life=tx.scan('agent_lifecycles')[0]['body']
        first_order=life['attempt_refs'][0]
        first_outcome=deepcopy(tx.get('repair_outcomes',first_order))
        assert first_outcome['body']['postcheck_status']=='rejected'
        first_receipt=tx.get('receipts',first_outcome['body']['return_result']['output']['receipt_ref'])
        assert first_receipt['body']['result']['execution_status']=='succeeded'
        assert first_receipt['body']['result']['error'] is None
        assert life['phase']=='diagnosing'
        bounds=tx.get('lifecycle_budgets','lineage:lineage/a')['body']
        assert bounds['spent']['repair_attempts']==1 and bounds['limits']['repair_attempts']==2
        proof_ref=first_outcome['body']['postcheck_evidence_ref']
        proof=deepcopy(tx.get('repair_postchecks',proof_ref))
        assert proof['body']['postcheck_status']=='rejected'
        assert proof['body']['postcheck_ref']=='test.probe-file@1'
        assert proof['body']['receipt_ref']==first_receipt['key']
    assert effects==['probe']
    await drain(worker)
    with repository.transaction() as tx:
        life=tx.scan('agent_lifecycles')[0]['body']
        assert life['phase']=='fulfilled'
        orders=[tx.get('repair_orders',ref)['body'] for ref in life['attempt_refs']]
        assert [order['action_ref'] for order in orders]==['test.probe@1','test.restore@1']
        assert [event['kind'] for event in life['events']]==['disposition_confirmed','known_action','claimed',
            'postcheck_fail_with_budget','known_action','claimed','postcheck_pass','node_accepted']
        decisions=sorted((r['body'] for r in tx.scan('lifecycle_decisions')),key=lambda d:d['expected_revision'])
        assert decisions[1]['evidence_refs']==[life['source_receipt'],first_receipt['key'],proof_ref]
        selection=tx.get('repair_strategy_selections',decisions[1]['decision_ref'])['body']
        assert selection['rule']['predicate']=='postcheck_rejected'
        assert selection['same_failure_count']==0
        assert tx.get('repair_failure_fingerprints',first_order)['body']['failure_fingerprint']==life['issue_fingerprint']
        assert len(tx.scan('repair_failure_fingerprints'))==1
        assert tx.scan('recovery_plans')[0]['body']['revalidate_nodes']==['N01','N02','N03']
        recovery=next(row['body'] for row in tx.scan('jobs') if row['key'].startswith('recovery-work:'))
        assert recovery['result']['graph_result']['recovery_acceptance']['accepted'] is True
        for before in (first_receipt,first_outcome,proof):
            kind='receipts' if before is first_receipt else 'repair_outcomes' if before is first_outcome else 'repair_postchecks'
            assert tx.get(kind,before['key'])==before
        for ref,before in original_receipts.items():
            assert tx.get('receipts',ref)==before
        for bounds in tx.scan('lifecycle_budgets'):
            assert bounds['body']['spent']['repair_attempts']==2
            assert bounds['body']['spent']['child_jobs']==9
            assert bounds['body']['spent']['diagnosis_calls']==0
            assert not any(bounds['body']['reserved'].values())
        assert not tx.scan('lifecycle_blockers') and not tx.scan('agent_team_tasks')
    assert effects==['probe','restore']
    assert raw.read_bytes()==b'original raw\x00\xff'
    assert [node for _,activation,node,_ in seen if activation.startswith('recovery-work:')]==list(NODE_GROUPS['work'][1:])


@pytest.mark.asyncio
async def test_n02_recovery_requires_fresh_source_postcheck_after_successful_delivery(repository,clock,tmp_path):
    # A successful N10 cannot replace a fresh observation of the N02 effect.
    worker,seen,effects,raw=strategy_assembly(repository,tmp_path,source_node='N02',
        recovery_postcheck=False)
    await drain(worker)
    clock.advance(1.0)
    await drain(worker)
    with repository.transaction() as tx:
        life=tx.scan('agent_lifecycles')[0]['body']
        assert life['key']['node_id']=='N02' and life['phase']=='draining'
        assert [event['kind'] for event in life['events']]==[
            'disposition_confirmed','known_action','claimed','postcheck_fail_with_budget',
            'timer_due','known_action','claimed','postcheck_pass','node_failed']
        assert [tx.get('repair_outcomes',ref)['body']['postcheck_status']
            for ref in life['attempt_refs']]==['rejected','accepted']
        recovery=next(row['body'] for row in tx.scan('jobs') if row['key'].startswith('recovery-work:'))
        graph=recovery['result']['graph_result']
        assert graph['recovery_acceptance']['source_node']=='N02'
        assert graph['recovery_acceptance']['execution_review_accepted'] is True
        assert graph['recovery_acceptance']['accepted'] is False
        assert graph['recovery_acceptance']['reason']=='node_postcheck_rejected'
        assert graph['recovery_dependency_failures']=={}
        assert tx.get('receipts',graph['result_refs']['N10'])['body']['result']['execution_status']=='succeeded'
        assert len(tx.scan('repair_orders'))==2 and len(tx.scan('recovery_plans'))==1
        assert not any(event['kind']=='node_accepted' for event in life['events'])
    assert effects==['probe','restore']
    assert [node for _,activation,node,_ in seen if activation.startswith('recovery-work:')]==[
        'N01','N02','N03','N04','N05','N06','N07','N08','N09','N10']
    assert raw.read_bytes()==b'original raw\x00\xff'
