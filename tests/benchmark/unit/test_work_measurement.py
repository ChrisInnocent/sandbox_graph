"""P8.3 / ORC-024-027,042,059-064: N04-N07 observation before load, stop after steady."""
from sandbox_bench.session import BenchSession
from tests.benchmark.scenarios.test_scenarios import case

CONFIG = {'product': 'agentenv', 'api_url': 'http://localhost:1', 'data_url': 'http://localhost:1'}
RESOURCES = {'interval_s': 1, 'window_s': 2, 'max_wait_s': 5, 'max_gap_s': 2,
             'min_samples': 2, 'inventory_ref': 'inventory'}


async def test_observation_is_not_requested_without_a_resource_contract(tmp_path):
    session = BenchSession(case('B01.TEMPLATE'), CONFIG, tmp_path)
    assert (await session._N04()) == {'status': 'completed', 'observation': 'not_requested'}


async def test_n04_blocks_without_a_sampler_when_resources_are_declared(tmp_path):
    session = BenchSession(case('B01.TEMPLATE', resources=RESOURCES), CONFIG, tmp_path)
    assert (await session._N04())['status'] == 'blocked'


async def test_n05_requires_an_active_observation_before_load(tmp_path):
    session = BenchSession(case('B01.TEMPLATE', resources=RESOURCES), CONFIG, tmp_path)
    result = await session._N05()
    assert result['status'] == 'blocked' and result['reason'] == 'active_observation_required_before_load'


async def test_n07_is_a_noop_when_no_observation_was_ever_started(tmp_path):
    session = BenchSession(case('B01.TEMPLATE'), CONFIG, tmp_path)
    assert (await session._N07()) == {'status': 'completed'}
