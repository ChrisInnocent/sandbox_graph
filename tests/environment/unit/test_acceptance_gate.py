from datetime import datetime,timezone,timedelta
from types import SimpleNamespace
import pytest
from sandbox_graph.environment.acceptance import AcceptanceService,observed_fingerprint
from sandbox_graph.environment.contracts import EnvironmentReport,CheckObservation
from sandbox_graph.services.workflows import WorkflowHandlers
from tests.environment.unit.test_canary import prepare,Adapter

@pytest.mark.asyncio
async def test_acceptance_requires_real_functional_cleanup_bytes_and_fresh_environment(tmp_path):
    repo,runner,op,store=prepare(tmp_path,Adapter());await runner.submit(op)
    request=runner._request(op)
    with repo.transaction() as tx:
        from sandbox_graph.environment.contracts import EnvironmentDeclaration
        declaration=EnvironmentDeclaration.model_validate(tx.get('environment_declarations',request.declaration_hash)['body'])
    report=EnvironmentReport(declaration_hash=declaration.declaration_hash,source_set=declaration.source_set,
        profile=declaration.profile,readiness={'n1':'ready'},observations=(CheckObservation(check_id='configuration',
            rule_ref='probe.v1',node_id='n1',component='server',status='satisfied',observed={'sha256':'a'*64},
            observed_at=datetime.now(timezone.utc).isoformat(),boot_id='boot'),),
        valid_until=(datetime.now(timezone.utc)+timedelta(minutes=2)).isoformat())
    service=AcceptanceService(repo,store=store)
    ref=service.issue(declaration,report,scope=op.scope,node_ids=('n1',),token=runner.token)
    assert service.verify(ref,current_declaration=declaration,current_report=report,node_ids=('n1',))
    changed=report.model_copy(update={'observations':(report.observations[0].model_copy(update={'boot_id':'new'}),)})
    with pytest.raises(ValueError,match='drift'):service.verify(ref,current_declaration=declaration,current_report=changed,node_ids=('n1',))

@pytest.mark.asyncio
async def test_t0_requires_acceptance_verifier_before_allocating_work(tmp_path):
    repo,_,_,_=prepare(tmp_path,Adapter())
    workflow=WorkflowHandlers(repo,deadline=1200.)
    case=SimpleNamespace(group_id='g',capability='supported',adapter_verified=True,
        method_id='B01',environment_ref='environment-acceptance:unresolved')
    workflow._plan=lambda context:SimpleNamespace(cases=(case,))
    result=await workflow.bind_methods(SimpleNamespace(scope='g'))
    assert result.execution_status=='blocked' and result.error.code=='ENVIRONMENT_ACCEPTANCE_REQUIRED'
    with repo.transaction() as tx:assert not tx.scan('work_instances')
