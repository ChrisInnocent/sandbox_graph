import os
import pytest
from sandbox_graph.testing.environment_acceptance import run_environment_acceptance

@pytest.mark.live
@pytest.mark.postgres
@pytest.mark.asyncio
@pytest.mark.parametrize('product',['cubesandbox','kuasar','agentenv'])
@pytest.mark.parametrize('topology',['single','multi'])
async def test_real_product_preparation_and_reentry(product,topology):
    name='SANDBOX_GRAPH_TEST_ENV_'+product.upper()+'_'+topology.upper()
    path=os.environ.get(name)
    if not path:pytest.skip(name+' is not supplied')
    result=await run_environment_acceptance(path,product=product,topology=topology)
    assert result['status']=='passed'
