"""ST-15: actual StateGraph/Worker stop chain with independent node trace."""
import pytest

from sandbox_graph.interfaces.service import RunService
from sandbox_graph.planning.compiler import PlanCompiler
from sandbox_graph.services.lifecycle_disposition import DispositionVersions
from tests.orchestration.graphs.test_work_remediation import assembly


@pytest.mark.asyncio
@pytest.mark.parametrize('failure',['N01','N04','N05','N07','N09'])
async def test_real_worker_stop_chain(repository,failure):
    worker,_,seen,repairs=assembly(repository,failure=failure)
    assert await worker.run_once()
    RunService(repository,PlanCompiler([])).cancel('stop','run/a','requested')
    target=None
    for _ in range(32):
        worker.pump_outbox()
        with repository.transaction() as tx:
            matches=[r for r in tx.scan('lifecycle_stop_requests') if r['body']['scope_ref']=='work/a'
                and r['body']['request_id']=='cancel:stop']
            if matches:
                assert len(matches)==1
                target=matches[0]['key']
                status=tx.get('lifecycle_stop_status',target)['body']
                if status['status']=='closed':
                    DispositionVersions.verify_stop_in(tx,tx.get('lifecycle_stop_proofs',status['proof_ref'])['body'])
                    break
        assert await worker.run_once(), 'queue exhausted without exact target closure'
    else:
        pytest.fail('bounded advancement never closed original stop target')
    assert target and repairs==[]
    trace=[node for activation,node in seen if activation=='original/a']
    assert trace.index('N07') < trace.index('N09') < trace.index('N10')
    assert not [activation for activation,_ in seen if activation!='original/a']
    with repository.transaction() as tx:
        assert tx.scan('work_deliveries')
        assert not tx.scan('repair_orders')
