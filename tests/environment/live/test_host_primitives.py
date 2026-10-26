import os
import pytest
from sandbox_graph.environment.cli import read_json
from sandbox_graph.testing.host_acceptance import run_host_acceptance


@pytest.mark.live
@pytest.mark.asyncio
async def test_actual_linux_ssh_systemd_proc_kvm_primitives():
    path=os.environ.get('SANDBOX_GRAPH_TEST_HOST_PRIMITIVES')
    if not path:pytest.skip('SANDBOX_GRAPH_TEST_HOST_PRIMITIVES contract not supplied')
    result=await run_host_acceptance(read_json(path))
    assert result['status']=='passed',result
