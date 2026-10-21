"""P8.2 / ORC-015,018,024,026,027: N00-N03 input guard and versioned evidence gate."""
import pytest

from sandbox_bench.session import BenchSession
from tests.benchmark.scenarios.test_scenarios import case

CONFIG = {'product': 'agentenv', 'api_url': 'http://localhost:1', 'data_url': 'http://localhost:1'}


async def test_n00_rejects_a_product_mismatch(tmp_path):
    session = BenchSession(case('B01.TEMPLATE'), {**CONFIG, 'product': 'kuasar'}, tmp_path)
    with pytest.raises(ValueError, match='product mismatch'):
        await session._N00()


async def test_n00_accepts_a_matching_product(tmp_path):
    session = BenchSession(case('B01.TEMPLATE'), CONFIG, tmp_path)
    assert (await session._N00())['status'] == 'completed'


async def test_n01_blocks_without_versioned_environment_evidence(tmp_path):
    session = BenchSession(case('B01.TEMPLATE'), CONFIG, tmp_path)
    assert (await session._N01())['status'] == 'blocked'
