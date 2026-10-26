import json
from pathlib import Path
import pytest


def test_fixed_gate_profiles_and_commands_exist():
    from sandbox_graph.runtime.lifecycle_cli import parser
    root=Path(__file__).resolve().parents[3]
    for name in ('development','persistent','isolation','release'):
        value=json.loads((root/'config/gates'/f'lifecycle-{name}.json').read_text())
        assert value['required_ids'] and all((root/path).is_file() for path in value['paths'])
        args=parser().parse_args(['gates','run','--profile',name,'--output','/tmp/gate-test'])
        assert args.profile==name
    for name in ('status','request-repair','request-change','decision','cancel'):
        assert name in parser().format_help()


def test_persistent_gate_missing_dsn_is_blocked_before_subprocess(tmp_path,monkeypatch):
    from sandbox_graph.runtime.lifecycle_cli import run_profile
    monkeypatch.delenv('SANDBOX_GRAPH_TEST_POSTGRES_DSN',raising=False)
    result=run_profile('persistent',tmp_path/'gate')
    assert result['status']=='blocked' and result['verification_level']=='local_only'
    assert not (tmp_path/'gate'/'run.log').exists()


def test_control_cancel_is_scoped_audited_idempotent_and_cannot_reuse_request(repository):
    from sandbox_graph.runtime.lifecycle_cli import LifecycleCommands
    from sandbox_graph.services.lifecycle_stop import LifecycleStopService
    from tests.orchestration.unit.test_scope_lifecycle import seeded
    life,token=seeded(repository,'C2')
    command={'request_id':'cancel/1','lifecycle_id':life.key.lifecycle_id,'expected_revision':life.revision,
        'principal':'operator','authorization_ref':'control/a','input':{'reason':'operator cancellation'}}
    service=LifecycleCommands(repository)
    with pytest.raises(PermissionError):service.execute('cancel',command)
    with repository.transaction() as tx:
        tx.put('lifecycle_control_authorizations','control/a',{'principal':'operator','lifecycle_id':life.key.lifecycle_id,
            'run_ref':life.key.run_ref,'scope_ref':life.key.scope_ref,'graph_ref':life.graph_ref,'plan_ref':life.plan_ref,
            'revoked':False,'expires_at':1100.,'actions':['cancel']})
    result=service.execute('cancel',command)
    assert result['status']=='cancel_requested' and service.execute('cancel',command)==result
    with pytest.raises(Exception,match='input changed'):
        service.execute('cancel',command|{'input':{'reason':'different'}})
    with repository.transaction() as tx:
        assert len(tx.scan('lifecycle_control_commands'))==1
        assert LifecycleStopService.policy_in(tx,life)=='cancel'


def test_operator_authority_is_rechecked_after_claim_before_decision(repository,monkeypatch):
    from sandbox_graph.runtime.lifecycle_cli import LifecycleCommands
    from sandbox_graph.services.agent_lifecycle import LifecycleService
    from tests.orchestration.unit.test_scope_lifecycle import seeded
    life,_=seeded(repository,'C2')
    command={'request_id':'decision/1','lifecycle_id':life.key.lifecycle_id,'expected_revision':life.revision,
        'principal':'operator','authorization_ref':'control/a','input':{'route':'needs_input','evidence_refs':[]}}
    with repository.transaction() as tx:
        tx.put('lifecycle_control_authorizations','control/a',{'principal':'operator','lifecycle_id':life.key.lifecycle_id,
            'run_ref':life.key.run_ref,'scope_ref':life.key.scope_ref,'graph_ref':life.graph_ref,'plan_ref':life.plan_ref,
            'revoked':False,'expires_at':1100.,'actions':['decision']})
    def claim(self,*args,**kwargs):
        with repository.transaction() as tx:
            row=tx.get('lifecycle_control_authorizations','control/a')
            tx.put('lifecycle_control_authorizations','control/a',row['body']|{'revoked':True})
    monkeypatch.setattr(LifecycleService,'claim',claim)
    monkeypatch.setattr(LifecycleService,'record_decision_in',lambda *a,**kw:pytest.fail('decision reached after revocation'))
    with pytest.raises(PermissionError,match='authorization'):LifecycleCommands(repository).execute('decision',command)
