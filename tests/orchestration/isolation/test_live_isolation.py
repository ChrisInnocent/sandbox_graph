import json
import os
from pathlib import Path
import pytest
from sandbox_graph.testing.isolation_acceptance import qualify

@pytest.mark.live
@pytest.mark.isolation
@pytest.mark.asyncio
async def test_real_isolation_canaries_and_original_process_cleanup():
    path=os.environ.get('SANDBOX_GRAPH_TEST_ISOLATION_CONTRACT')
    if not path:pytest.skip('SANDBOX_GRAPH_TEST_ISOLATION_CONTRACT not supplied')
    value=json.loads(Path(path).read_text())
    assert set(value)=={'rootfs','rootfs_hash','output','python'}
    result=await qualify(**value)
    assert result['status']=='pass' and result['backend_verified'],result
