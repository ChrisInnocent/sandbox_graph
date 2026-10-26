import pytest
from tests.orchestration.unit.test_scope_lifecycle import seeded


def command(life,mode,**kwargs):
    return {'request_id':'rollout:'+mode,'run_ref':life.key.run_ref,'expected_revision':None,
        'principal':'operator','authorization_ref':'rollout-auth','mode':mode,
        'scopes':[{'scope_ref':life.key.scope_ref,'node_ids':[life.key.node_id]}],**kwargs}


def authorize(repo,life):
    with repo.transaction() as tx:
        tx.put('lifecycle_rollout_authorizations','rollout-auth',{'principal':'operator',
            'run_ref':life.key.run_ref,'modes':['off','actions','scoped','candidate'],
            'revoked':False,'expires_at':1100.,'scope_refs':[life.key.scope_ref]})


@pytest.mark.parametrize('mode,allowed',[
    ('off',()),('actions',('action',)),('scoped',('action','model')),('candidate',('action','model','candidate'))])
def test_rollout_modes_are_scoped_audited_and_do_not_reset_budget(repository,mode,allowed):
    from sandbox_graph.services.lifecycle_rollout import LifecycleRolloutService
    from sandbox_graph.services.lifecycle_budgets import LifecycleBudgetService
    life,_=seeded(repository,'C2');authorize(repository,life)
    service=LifecycleRolloutService(repository)
    with repository.transaction() as tx:before=tx.scan('lifecycle_budgets')
    result=service.configure(command(life,mode))
    assert result==service.configure(command(life,mode))
    with repository.transaction() as tx:
        assert tx.scan('lifecycle_budgets')==before
        for kind in ('action','model','candidate'):
            if kind in allowed:service.assert_enabled_in(tx,life,kind)
            else:
                with pytest.raises(PermissionError,match='rollout'):service.assert_enabled_in(tx,life,kind)
        other=life.model_copy(update={'key':life.key.model_copy(update={'scope_ref':'another-scope'})})
        with pytest.raises(PermissionError,match='rollout'):service.assert_enabled_in(tx,other,'action')


def test_rollout_off_blocks_existing_decision_but_keeps_stop_protocol(repository):
    from sandbox_graph.services.lifecycle_rollout import LifecycleRolloutService
    from sandbox_graph.services.agent_lifecycle import LifecycleService
    from sandbox_graph.services.lifecycle_stop import LifecycleStopService
    life,token=seeded(repository,'C2');authorize(repository,life)
    LifecycleRolloutService(repository).configure(command(life,'off'))
    with repository.transaction() as tx:
        with pytest.raises(PermissionError,match='rollout'):
            LifecycleService.record_decision_in(tx,life.key.lifecycle_id,request_id='decision-off',
                expected_revision=life.revision,route='known_action',evidence_refs=(life.source_receipt,),token=token)
        assert LifecycleStopService.request_lifecycle_in(tx,request_id='stop-off',record=life,reason='disabled')


def test_rollout_authority_cas_and_input_conflict(repository):
    from sandbox_graph.services.lifecycle_rollout import LifecycleRolloutService
    from sandbox_graph.persistence.repository import Conflict
    life,_=seeded(repository,'C2');service=LifecycleRolloutService(repository)
    with pytest.raises(PermissionError):service.configure(command(life,'actions'))
    authorize(repository,life);service.configure(command(life,'actions'))
    with pytest.raises(Conflict):service.configure(command(life,'off'))
    with pytest.raises(Conflict):service.configure(command(life,'candidate',request_id='rollout:actions'))
    service.configure(command(life,'off',expected_revision=0))
    with repository.transaction() as tx:
        assert tx.get('lifecycle_rollouts',life.key.run_ref)['body']['mode']=='off'
