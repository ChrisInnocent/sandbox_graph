"""ALU-06: trusted applicability, persistent evidence and bounded strategy reuse."""
import pytest
from pydantic import ValidationError

from sandbox_graph.catalog.repair_actions import RepairActionCatalog, standard_repair_catalog
from tests.orchestration.graphs.test_work_remediation import assembly, drain


def rule(action='case.reconcile@1', *, predicate='source_error', codes=('BENCHMARK_STEP_FAILED',), preceding=None):
    from sandbox_graph.catalog.repair_actions import RepairApplicabilityRule
    return RepairApplicabilityRule(schema_version=1, rule_ref='test:' + action + ':' + predicate,
        node_id='N01', action_ref=action, predicate=predicate, error_codes=codes,
        preceding_action_ref=preceding)


def catalog(*rules):
    standard=standard_repair_catalog()
    entry=standard.resolve('case.reconcile','1','N01')
    return RepairActionCatalog((entry,), (), rules=rules)


@pytest.mark.parametrize('changes', [
    {'schema_version':True}, {'schema_version':'1'}, {'schema_version':2},
    {'predicate':'python'}, {'error_codes':()}, {'handler':'arbitrary'},
    {'predicate':'repair_failure'}, {'preceding_action_ref':'case.reconcile@1'},
])
def test_applicability_contract_rejects_unversioned_or_ambiguous_predicates(changes):
    from sandbox_graph.catalog.repair_actions import RepairApplicabilityRule
    body=rule().model_dump()
    with pytest.raises(ValidationError):
        RepairApplicabilityRule.model_validate(body | changes)


def test_rule_cannot_register_an_unavailable_or_different_node_action():
    with pytest.raises(ValueError):
        catalog(rule(action='fixture.repair@1'))


@pytest.mark.asyncio
async def test_available_action_without_matching_evidence_waits_without_model_or_repair(repository):
    worker, supervisor, _, repairs=assembly(repository)
    supervisor.repair_catalog=catalog(rule(codes=('SOME_OTHER_KNOWN_ERROR',)))
    await drain(worker)
    with repository.transaction() as tx:
        assert tx.scan('agent_lifecycles')[0]['body']['phase']=='waiting_decision'
        assert not tx.scan('repair_orders')
        assert tx.scan('lifecycle_blockers')[0]['body']['reason']=='no_applicable_rule_model_unavailable'
        before=len(tx.scan('jobs'))
    await drain(worker)
    with repository.transaction() as tx:
        assert len(tx.scan('jobs'))==before
    assert repairs==[]


@pytest.mark.asyncio
async def test_repeated_failure_does_not_gain_new_allowance_from_evidence_references(repository,clock):
    worker, supervisor, _, repairs=assembly(repository,repair_failures=10)
    supervisor.repair_catalog=catalog(rule())
    with repository.transaction() as tx:
        row=tx.get('repair_policies','policy/runtime')
        tx.put('repair_policies',row['key'],row['body'] | {'max_same_failure':1})
    await drain(worker)
    clock.advance(1.0)
    await drain(worker)
    with repository.transaction() as tx:
        assert tx.scan('agent_lifecycles')[0]['body']['phase']=='blocked'
        assert tx.scan('lifecycle_blockers')[0]['body']['reason']=='no_progress'
        assert len(tx.scan('repair_failure_fingerprints'))==1
        assert tx.get('lifecycle_budgets','lineage:lineage/a')['body']['spent']['repair_attempts']==1
    assert len(repairs)==1


def test_bootstrap_uses_one_trusted_catalog_for_graph_repair_supervisor_and_model_team(repository):
    from sandbox_graph.runtime.bootstrap import build_job_handlers
    from sandbox_graph.services.agent_team import AgentTeamService
    from tests.orchestration.unit.test_agent_team import configuration
    trusted=catalog(rule())
    team=AgentTeamService(repository, object(), config=configuration(), repair_catalog=trusted)
    handlers=build_job_handlers(repository,graph_ref='graph/a',saver_factory=lambda token:None,
        node_service_factory=lambda token,activation:{},review_service=object(),deadline=1100.0,
        lease_seconds=30,segment_seconds=20,lifecycle_bindings=[],model_team=team,
        agent_provider=object(),repair_catalog=trusted)
    assert handlers['work'].repair_catalog is trusted
    assert handlers['remedy'].repair_catalog is trusted
    assert handlers['lifecycle'].repair_catalog is trusted
    assert team.repair_catalog is trusted


def test_bootstrap_rejects_model_team_with_a_different_catalog(repository):
    from sandbox_graph.runtime.bootstrap import build_job_handlers
    from sandbox_graph.services.agent_team import AgentTeamService
    from tests.orchestration.unit.test_agent_team import configuration
    team=AgentTeamService(repository,object(),config=configuration(),repair_catalog=catalog(rule()))
    with pytest.raises(ValueError,match='same trusted repair catalog'):
        build_job_handlers(repository,graph_ref='graph/a',saver_factory=lambda token:None,
            node_service_factory=lambda token,activation:{},review_service=object(),deadline=1100.0,
            lease_seconds=30,segment_seconds=20,lifecycle_bindings=[],model_team=team,
            agent_provider=object(),repair_catalog=catalog(rule()))


@pytest.mark.asyncio
async def test_strategy_selection_and_decision_commit_atomically(repository,monkeypatch):
    from tests.orchestration.conftest import MemoryTransaction
    worker, _, _, _=assembly(repository)
    assert await worker.run_once()
    original=MemoryTransaction.put
    def cut(tx,kind,key,body,*args,**kwargs):
        if kind=='repair_strategy_selections':
            raise RuntimeError('cut during strategy selection')
        return original(tx,kind,key,body,*args,**kwargs)
    monkeypatch.setattr(MemoryTransaction,'put',cut)
    await drain(worker)
    with repository.transaction() as tx:
        assert not tx.scan('lifecycle_decisions')
        assert not [r for r in tx.scan('outbox') if r['body'].get('kind')=='decision_ready']
        assert not tx.scan('repair_orders')


@pytest.mark.parametrize('changes', [
    {'preceding_action_ref':None}, {'preceding_postcheck_ref':None},
    {'error_codes':('INVENTED_EXECUTION_ERROR',)}, {'schema_version':True},
])
def test_postcheck_rule_requires_exact_predecessor_postcheck_without_execution_error(changes):
    from sandbox_graph.catalog.repair_actions import RepairApplicabilityRule
    body={'schema_version':1,'rule_ref':'test.postcheck-rule@1','node_id':'N01',
        'action_ref':'case.reconcile@1','predicate':'postcheck_rejected',
        'preceding_action_ref':'case.reconcile@1','preceding_postcheck_ref':'case.known_operation@1'}
    # The valid contract must represent successful execution without error codes.
    assert RepairApplicabilityRule.model_validate(body).error_codes==()
    with pytest.raises(ValidationError):
        RepairApplicabilityRule.model_validate(body | changes)


def test_postcheck_rule_cannot_name_another_actions_postcheck():
    from sandbox_graph.catalog.repair_actions import RepairApplicabilityRule
    invalid=RepairApplicabilityRule(schema_version=1,rule_ref='test.wrong-postcheck@1',node_id='N01',
        action_ref='case.reconcile@1',predicate='postcheck_rejected',
        preceding_action_ref='case.reconcile@1',preceding_postcheck_ref='resource.closed_disposition@1')
    with pytest.raises(ValueError,match='postcheck'):
        catalog(invalid)


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['missing_proof','wrong_action','wrong_postcheck','wrong_receipt',
    'unknown_effect','unknown_postcheck','wrong_producer','missing_outcome','outcome_accepted'])
async def test_postcheck_rule_rejects_missing_or_mismatched_persisted_evidence(repository,tmp_path,change):
    from copy import deepcopy
    from sandbox_graph.domain.models import canonical_hash
    from sandbox_graph.domain.agent_lifecycle import RepairPolicy
    from sandbox_graph.services.agent_lifecycle import LifecycleService,decode
    from sandbox_graph.services.repair_strategy import select_strategy_in
    from tests.orchestration.graphs.test_work_strategy import strategy_assembly
    worker,_,_,_=strategy_assembly(repository,tmp_path,a_postcheck_failure=True)
    for _ in range(10):
        await worker.run_once()
        with repository.transaction() as tx:
            if tx.scan('repair_outcomes'):
                break
    with repository.transaction() as tx:
        record=LifecycleService.load_in(tx,tx.scan('agent_lifecycles')[0]['key'])
        policy=decode(RepairPolicy,tx.get('repair_policies',record.policy_ref)['body'])
        trusted=worker.handlers['lifecycle'].repair_catalog
        selected,_=select_strategy_in(tx,record,policy,trusted)
        assert selected['action_ref']=='test.restore@1'
        order_ref=record.attempt_refs[-1]
        outcome=deepcopy(tx.get('repair_outcomes',order_ref)['body'])
        proof_ref=outcome['postcheck_evidence_ref']
        proof=deepcopy(tx.get('repair_postchecks',proof_ref)['body'])
        if change=='missing_proof':
            tx.delete('repair_postchecks',proof_ref)
        elif change=='missing_outcome':
            tx.delete('repair_outcomes',order_ref)
        elif change=='outcome_accepted':
            tx.put('repair_outcomes',order_ref,outcome | {'postcheck_status':'accepted'})
        else:
            fields={'wrong_action':('action_ref','test.restore@1'),
                'wrong_postcheck':('postcheck_ref','test.restore-file@1'),
                'wrong_receipt':('receipt_ref',record.source_receipt),
                'unknown_effect':('effect_certainty','unknown'),
                'unknown_postcheck':('postcheck_status','unknown'),
                'wrong_producer':('producer','model')}
            name,value=fields[change]
            proof[name]=value
            replacement='repair-postcheck:' + canonical_hash(proof)
            tx.put('repair_postchecks',replacement,proof,immutable=True)
            tx.put('lifecycle_evidence',replacement,{'lifecycle_id':record.key.lifecycle_id,
                'kind':'repair_postcheck'},immutable=True)
            failure=tx.get('repair_failure_fingerprints',order_ref)['body']
            tx.put('repair_failure_fingerprints',order_ref,failure | {'postcheck_evidence_ref':replacement})
            tx.put('repair_outcomes',order_ref,outcome | {'postcheck_evidence_ref':replacement})
        selected,_=select_strategy_in(tx,record,policy,trusted)
        assert selected is None
